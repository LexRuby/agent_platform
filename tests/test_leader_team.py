"""leader_team 模块测试：主理人预置团队成员。

测试隔离原则：
- sidecar 文件 → tmp_path（AGENTFORGE_AGENT_TYPES_FILE / AGENTFORGE_LEADER_TEAMS_FILE）
- HTTP → TestClient（ASGI 进程内，模拟官方 agent API）
- LLM → monkeypatch app.leader_team.llm_chat_json
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.leader_team as lt
from app.agent_type import AgentTypeMiddleware, AgentTypeStore
from app.leader_team import (
    LeaderTeamMiddleware,
    LeaderTeamStore,
    build_team_section,
    extract_team_members,
    strip_team_section,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_AGENT_TYPES_FILE", str(tmp_path / "types.json"))
    monkeypatch.setenv("AGENTFORGE_LEADER_TEAMS_FILE", str(tmp_path / "teams.json"))
    return tmp_path


def _make_stack(env):
    """AgentTypeMiddleware 内层 + LeaderTeamMiddleware 外层（与生产同构）。

    内层 mock 的响应结构一律取自 tests/official_contract.py（真实契约），
    禁止手写 {"id": ...} 之类内联结构——曾因 mock 失真导致线上 bug 测试全绿。
    """
    from tests.official_contract import (
        agent_item,
        list_agent_response,
        post_agent_response,
    )

    inner = FastAPI()
    db = {"next": 1, "agents": {}}

    @inner.post("/agent/")
    async def create(body: dict):
        aid = f"a{db['next']}"
        db["next"] += 1
        db["agents"][aid] = body
        return post_agent_response(aid)

    @inner.get("/agent/")
    async def list_():
        return list_agent_response(
            [agent_item(k, v) for k, v in db["agents"].items()],
        )

    @inner.patch("/agent/{aid}")
    async def update(aid: str, body: dict):
        db["agents"][aid].update(body)
        return {"id": aid, "data": db["agents"][aid]}

    @inner.delete("/agent/{aid}")
    async def remove(aid: str):
        db["agents"].pop(aid, None)
        return {"ok": True}

    # 生产环境 router 挂在官方 app（内层），测试同构
    inner.include_router(lt.leader_team_router)

    ts = AgentTypeStore(str(env / "types.json"))
    ls = LeaderTeamStore(str(env / "teams.json"))
    app = AgentTypeMiddleware(inner, store=ts)
    app2 = LeaderTeamMiddleware(app, store=ls, type_store=ts)
    return TestClient(app2), ts, ls, db


@pytest.fixture
def stack(env):
    return _make_stack(env)


def _mk_members(client, n=2):
    ids = []
    for i in range(n):
        r = client.post(
            "/agent/", json={"name": f"成员{i}", "agent_type": "member"},
        )
        ids.append(r.json()["agent_id"])
    return ids


class TestStore:
    def test_load_missing(self, env):
        assert LeaderTeamStore(env / "nope.json").load() == {}

    def test_set_get_delete(self, env):
        s = LeaderTeamStore(env / "t.json")
        s.set("L", ["m1", "m2"])
        assert s.get("L") == ["m1", "m2"]
        s.delete("L")
        assert s.get("L") == []

    def test_set_dedup_and_empty(self, env):
        s = LeaderTeamStore(env / "t.json")
        s.set("L", ["m1", "m1", "m2"])
        assert s.get("L") == ["m1", "m2"]
        s.set("L", [])
        assert s.get("L") == []

    def test_corrupted_degrades(self, env):
        (env / "t.json").write_text("{bad", encoding="utf-8")
        assert LeaderTeamStore(env / "t.json").load() == {}


class TestSection:
    def test_build_format(self):
        s = build_team_section([
            {"name": "研究员", "description": "政策专家"},
            {"name": "助手", "description": ""},
        ])
        assert "## 预置团队成员" in s
        assert "- 研究员：政策专家" in s
        assert "- 助手" in s and "助手：" not in s

    def test_strip(self):
        prompt = "基础设定\n\n## 预置团队成员\n- 旧成员"
        assert strip_team_section(prompt) == "基础设定"
        assert strip_team_section("无段落") == "无段落"


class TestExtract:
    def test_objects(self):
        body, ms = extract_team_members(
            '{"name":"L","team_members":[{"id":"m1","name":"甲","description":"d"}]}'.encode(),
        )
        assert ms == [{"id": "m1", "name": "甲", "description": "d"}]
        assert "team_members" not in json.loads(body)

    def test_plain_ids(self):
        _, ms = extract_team_members(b'{"team_members":["m1","m2"]}')
        assert ms[0] == {"id": "m1", "name": "m1"[:8], "description": ""}

    def test_missing_key(self):
        body, ms = extract_team_members(b'{"name":"L"}')
        assert ms is None

    def test_non_json(self):
        body, ms = extract_team_members(b"raw")
        assert ms is None and body == b"raw"

    def test_non_list(self):
        _, ms = extract_team_members(b'{"team_members":"x"}')
        assert ms is None


class TestMiddleware:
    def _get_agent(self, client, aid):
        """模拟前端使用逻辑：创建后从 GET 列表回读（POST 响应只有 agent_id）。"""
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        return agents[aid]

    def test_post_injects_prompt_and_sidecar(self, stack):
        client, ts, ls, db = stack
        m1, m2 = _mk_members(client)
        r = client.post("/agent/", json={
            "name": "主理人", "agent_type": "leader",
            "system_prompt": "你是主理人。",
            "team_members": [
                {"id": m1, "name": "研究员", "description": "政策"},
                {"id": m2, "name": "志愿兵", "description": ""},
            ],
        })
        assert r.status_code == 200
        lid = r.json()["agent_id"]
        # 回读验证（真实契约：POST 响应只有 agent_id，不返回 data）
        stored = self._get_agent(client, lid)
        sp = stored["data"]["system_prompt"]
        assert "team_members" not in stored["data"]
        assert "你是主理人。" in sp and "## 预置团队成员" in sp
        assert "- 研究员：政策" in sp
        assert ls.get(lid) == [m1, m2]

    def test_post_without_team_members_passthrough(self, stack):
        client, _, ls, _ = stack
        r = client.post("/agent/", json={"name": "L", "agent_type": "leader"})
        assert ls.load() == {}
        assert r.status_code == 200

    def test_post_member_with_team_members_ignored(self, stack):
        client, _, ls, _ = stack
        m1, = _mk_members(client, 1)
        r = client.post("/agent/", json={
            "name": "普通", "agent_type": "member",
            "team_members": [{"id": m1, "name": "x", "description": ""}],
        })
        stored = self._get_agent(client, r.json()["agent_id"])
        assert "## 预置团队成员" not in (stored["data"].get("system_prompt") or "")
        assert ls.load() == {}

    def test_post_rejects_leader_as_member(self, stack):
        client, _, ls, _ = stack
        lid = client.post(
            "/agent/", json={"name": "L", "agent_type": "leader"},
        ).json()["agent_id"]
        r = client.post("/agent/", json={
            "name": "L2", "agent_type": "leader",
            "team_members": [{"id": lid, "name": "L", "description": ""}],
        })
        stored = self._get_agent(client, r.json()["agent_id"])
        assert "## 预置团队成员" not in (stored["data"].get("system_prompt") or "")
        assert ls.load() == {}

    def test_get_injects_team_members(self, stack):
        client, _, ls, _ = stack
        m1, = _mk_members(client, 1)
        lid = client.post("/agent/", json={
            "name": "L", "agent_type": "leader",
            "team_members": [{"id": m1, "name": "x", "description": ""}],
        }).json()["agent_id"]
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[lid]["team_members"] == [m1]
        assert "team_members" not in agents[m1]

    def test_patch_rewrites_section(self, stack):
        client, _, ls, _ = stack
        m1, m2 = _mk_members(client)
        lid = client.post("/agent/", json={
            "name": "L", "agent_type": "leader", "system_prompt": "基础",
            "team_members": [
                {"id": m1, "name": "a", "description": ""},
                {"id": m2, "name": "b", "description": ""},
            ],
        }).json()["agent_id"]
        r = client.patch(f"/agent/{lid}", json={
            "system_prompt": "新基础\n\n## 预置团队成员\n- 旧的",
            "team_members": [{"id": m2, "name": "b2", "description": "d"}],
        })
        sp = r.json()["data"]["system_prompt"]
        assert sp.count("## 预置团队成员") == 1
        assert "旧的" not in sp and "新基础" in sp and "- b2：d" in sp
        assert ls.get(lid) == [m2]

    def test_patch_empty_clears(self, stack):
        # 约束：清空名单时前端需带 system_prompt（编辑对话框总是全量提交），
        # 否则中间件无法改写存储中的既有提示词
        client, _, ls, _ = stack
        m1, = _mk_members(client, 1)
        lid = client.post("/agent/", json={
            "name": "L", "agent_type": "leader", "system_prompt": "基础",
            "team_members": [{"id": m1, "name": "x", "description": ""}],
        }).json()["agent_id"]
        r = client.patch(f"/agent/{lid}", json={
            "team_members": [], "system_prompt": "基础\n\n## 预置团队成员\n- x",
        })
        assert ls.get(lid) == []
        sp = db_sp(r)
        assert "## 预置团队成员" not in sp
        assert sp == "基础"

    def test_delete_cleans_sidecar(self, stack):
        client, _, ls, _ = stack
        m1, = _mk_members(client, 1)
        lid = client.post("/agent/", json={
            "name": "L", "agent_type": "leader",
            "team_members": [{"id": m1, "name": "x", "description": ""}],
        }).json()["agent_id"]
        client.delete(f"/agent/{lid}")
        assert ls.get(lid) == []

    def test_non_agent_paths_passthrough(self, stack):
        client, _, _, _ = stack
        assert client.get("/nonexistent").status_code == 404


def db_sp(r):
    d = r.json().get("data", r.json())
    return d.get("system_prompt") or ""


class TestRecommend:
    def _setup(self, stack):
        client, _, _, _ = stack
        _mk_members(client, 2)
        agents = client.get("/agent/").json()["agents"]
        return client, agents

    def test_empty_context(self, stack):
        client, agents = self._setup(stack)
        r = client.post(
            "/agent/recommend-members",
            json={"system_prompt": "", "task_topic": "", "members": agents},
        )
        assert r.json()["reason"] == "empty-input"

    def test_no_candidates(self, stack):
        client, _ = self._setup(stack)
        r = client.post(
            "/agent/recommend-members",
            json={"task_topic": "高考咨询", "members": []},
        )
        assert r.json()["reason"] == "no-candidates"

    def test_recommend_success(self, stack, monkeypatch):
        client, agents = self._setup(stack)
        by_name = {a["data"]["name"]: a for a in agents}
        target = by_name["成员0"]["id"]

        async def fake_llm(prompt, system=None):
            return {"recommendations": [
                {"id": target, "reason": "相关"},
                {"id": "nonexistent", "reason": "应被过滤"},
            ]}

        monkeypatch.setattr(lt, "llm_chat_json", fake_llm)
        r = client.post(
            "/agent/recommend-members",
            json={"task_topic": "政策分析", "members": agents},
        )
        d = r.json()
        assert d["fallback"] is False
        assert len(d["recommendations"]) == 1
        assert d["recommendations"][0]["id"] == target
        assert d["recommendations"][0]["name"] == "成员0"
        assert d["recommendations"][0]["reason"] == "相关"

    def test_recommend_llm_failure_fallback(self, stack, monkeypatch):
        client, agents = self._setup(stack)

        async def bad_llm(prompt, system=None):
            raise RuntimeError("api down")

        monkeypatch.setattr(lt, "llm_chat_json", bad_llm)
        r = client.post(
            "/agent/recommend-members",
            json={"task_topic": "x", "members": agents},
        )
        d = r.json()
        assert d["fallback"] is True and d["recommendations"] == []

    def test_recommend_filters_leader_candidates(self, stack):
        client, agents = self._setup(stack)
        # 加一个 leader 候选——服务端应过滤
        lid = client.post(
            "/agent/", json={"name": "大A", "agent_type": "leader"},
        ).json()["agent_id"]
        agents_with_leader = agents + [{
            "id": lid, "data": {"name": "大A"}, "agent_type": "leader",
        }]
        r = client.post(
            "/agent/recommend-members",
            json={"task_topic": "x", "members": agents_with_leader},
        )
        # leader 候选被过滤但 member 候选仍在 → 不是 no-candidates
        assert r.json()["reason"] != "no-candidates"


class TestLLMUtils:
    def test_extract_json_plain(self):
        from app.llm_utils import _extract_json
        assert _extract_json('{"a": 1}') == {"a": 1}
        assert _extract_json('[1, 2]') == [1, 2]

    def test_extract_json_fenced(self):
        from app.llm_utils import _extract_json
        assert _extract_json('前置说明\n```json\n{"a": 1}\n```\n后置') == {"a": 1}
        assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_extract_json_embedded(self):
        from app.llm_utils import _extract_json
        assert _extract_json('结果是 {"a": {"b": 2}} 完毕') == {"a": {"b": 2}}

    def test_extract_json_invalid(self):
        from app.llm_utils import _extract_json
        with pytest.raises(ValueError):
            _extract_json("完全不是 JSON")


class TestCreateLeaderEndToEnd:
    """用户完整使用逻辑：建成员 → 建主理人（带成员）→ 回读验证。

    模拟前端真实调用序列，任何一层（agent_type / leader_team 中间件、
    官方契约解析）断裂都会在此暴露——针对"各层单测全绿、组合链路断裂"
    的事故形态（如 POST 响应 agent_id 解析失败导致类型与成员名单同时丢失）。
    """

    def test_full_flow(self, stack):
        client, ts, ls, db = stack
        # 1. 建两个小A 成员
        m_ids = []
        for i, (name, desc) in enumerate([
            ("高考志愿兵", "志愿填报专家"), ("政策研究员", "政策解读"),
        ]):
            r = client.post("/agent/", json={
                "name": name, "agent_type": "member",
                "invite_config": {"invitable": True, "invite_description": desc},
            })
            assert r.status_code == 200
            m_ids.append(r.json()["agent_id"])

        # 2. 建大A 主理人，预置上述成员
        r = client.post("/agent/", json={
            "name": "高考主理人", "agent_type": "leader",
            "system_prompt": "你是高考团队主理人。",
            "team_members": [
                {"id": m_ids[0], "name": "高考志愿兵", "description": "志愿填报专家"},
                {"id": m_ids[1], "name": "政策研究员", "description": "政策解读"},
            ],
        })
        assert r.status_code == 200
        lid = r.json()["agent_id"]

        # 3. 回读：类型、成员名单、提示词注入三件事同时成立
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        leader = agents[lid]
        assert leader["agent_type"] == "leader"
        assert leader["team_members"] == m_ids
        sp = leader["data"]["system_prompt"]
        assert "你是高考团队主理人。" in sp
        assert "## 预置团队成员" in sp
        assert "- 高考志愿兵：志愿填报专家" in sp
        assert "- 政策研究员：政策解读" in sp
        # 成员未被污染
        for mid in m_ids:
            assert agents[mid]["agent_type"] == "member"
            assert "team_members" not in agents[mid]
            assert "## 预置团队成员" not in (agents[mid]["data"].get("system_prompt") or "")

        # 4. 编辑主理人换成员名单：段落重写 + sidecar 更新
        r = client.patch(f"/agent/{lid}", json={
            "name": "高考主理人", "agent_type": "leader",
            "system_prompt": "你是高考团队主理人。",
            "team_members": [{"id": m_ids[0], "name": "高考志愿兵", "description": "志愿填报专家"}],
        })
        assert r.status_code == 200
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[lid]["team_members"] == [m_ids[0]]
        assert agents[lid]["data"]["system_prompt"].count("## 预置团队成员") == 1
