"""agent_version 模块测试：agent 版本封板（freeze / unfreeze / save-version / restore）。

测试隔离原则：
- sidecar 文件 → tmp_path（AGENTFORGE_AGENT_VERSIONS_FILE 等）
- HTTP → TestClient（ASGI 进程内，模拟官方 agent API）
- 官方端点 → monkeypatch agent_version._call_official（指向未包装的
  内层 mock app，与生产 _official_app 语义一致：恢复版本不走拦截链）

覆盖用户核心诉求：
- freeze 后 agent 有版本号，PATCH（自我迭代）被 403 拦截
- unfreeze 开放模式可编辑；save-version 保存新版本号
- restore 恢复历史版本（冻结中 = 显式授权，也可执行）
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.agent_version as av
from app.agent_type import AgentTypeMiddleware, AgentTypeStore
from app.agent_version import (
    AgentVersionMiddleware,
    AgentVersionStore,
)
from app.leader_team import LeaderTeamMiddleware, LeaderTeamStore

U = {"X-User-ID": "u1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTFORGE_AGENT_VERSIONS_FILE", str(tmp_path / "versions.json"),
    )
    monkeypatch.setenv("AGENTFORGE_AGENT_TYPES_FILE", str(tmp_path / "types.json"))
    monkeypatch.setenv("AGENTFORGE_LEADER_TEAMS_FILE", str(tmp_path / "teams.json"))
    return tmp_path


def _make_stack(env, monkeypatch, storage=None):
    """与生产同构的中间件链：Version(LeaderTeam(AgentType(官方)))。

    storage：可选，挂到 inner app 的 state.storage（模拟生产
    create_app 的 app.state.storage，供 freeze 收集团队图纸）。

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

    inner.include_router(av.agent_version_router)
    if storage is not None:
        inner.state.storage = storage

    # _call_official 指向未包装的内层 app（生产语义：绕过拦截链）
    async def fake_call(method, path, user_id, json_body=None, params=None):
        transport = httpx.ASGITransport(app=inner)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t", timeout=10.0,
        ) as c:
            return await c.request(
                method, path, json=json_body, params=params,
                headers={"X-User-ID": user_id},
            )

    monkeypatch.setattr(av, "_call_official", fake_call)

    ts = AgentTypeStore(str(env / "types.json"))
    ls = LeaderTeamStore(str(env / "teams.json"))
    vs = AgentVersionStore(str(env / "versions.json"))
    app = AgentTypeMiddleware(inner, store=ts)
    app = LeaderTeamMiddleware(app, store=ls, type_store=ts)
    app = AgentVersionMiddleware(app, store=vs)
    return TestClient(app), vs, db


@pytest.fixture
def stack(env, monkeypatch):
    return _make_stack(env, monkeypatch)


def _create_agent(client, name="测试专家", prompt="你是测试专家"):
    r = client.post(
        "/agent/", json={"name": name, "system_prompt": prompt, "agent_type": "member"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["agent_id"]


class TestStore:
    def test_record_default_empty(self, env):
        s = AgentVersionStore(env / "v.json")
        rec = s.record("nobody")
        assert rec == {"frozen": False, "current_version": None, "versions": []}

    def test_add_version_increments_and_dedup(self, env):
        s = AgentVersionStore(env / "v.json")
        v1 = s.add_version("a1", {"name": "x", "system_prompt": "p1"})
        assert v1["version"] == 1
        # 内容相同 → 复用最新版本，不产生冗余版本号
        v1b = s.add_version("a1", {"name": "x", "system_prompt": "p1"})
        assert v1b["version"] == 1
        assert len(s.record("a1")["versions"]) == 1
        v2 = s.add_version("a1", {"name": "x", "system_prompt": "p2"})
        assert v2["version"] == 2
        assert len(s.record("a1")["versions"]) == 2

    def test_snapshot_only_config_fields(self, env):
        """快照只保留 AgentData 配置字段，运行时元数据不入档。"""
        s = AgentVersionStore(env / "v.json")
        entry = s.add_version("a1", {
            "name": "x", "system_prompt": "p",
            "invite_config": {"invitable": True},
            "id": "a1", "user_id": "u1", "updated_at": "t",
            "agent_type": "member", "version": {"frozen": True},
        })
        assert set(entry["data"]) == {"name", "system_prompt", "invite_config"}

    def test_get_version_and_delete(self, env):
        s = AgentVersionStore(env / "v.json")
        s.add_version("a1", {"name": "x", "system_prompt": "p1"})
        s.add_version("a1", {"name": "x", "system_prompt": "p2"})
        assert s.get_version("a1", 2)["data"]["system_prompt"] == "p2"
        assert s.get_version("a1", 99) is None
        s.delete("a1")
        assert s.record("a1")["versions"] == []

    def test_corrupted_degrades(self, env):
        (env / "v.json").write_text("{bad", encoding="utf-8")
        s = AgentVersionStore(env / "v.json")
        assert s.is_frozen("a1") is False

    def test_save_empty_record_not_persisted(self, env):
        s = AgentVersionStore(env / "v.json")
        s.save("a1", {"frozen": False, "current_version": None, "versions": []})
        assert "a1" not in s.load()


class TestFreezeUnfreeze:
    def test_freeze_creates_version_and_blocks_patch(self, stack):
        """核心诉求：freeze → 有版本号 → PATCH（自我迭代）被 403 拦截。"""
        client, _, db = stack
        aid = _create_agent(client)
        r = client.post(f"/agent/{aid}/freeze", headers=U, json={"label": "首版"})
        assert r.status_code == 200, r.text
        assert r.json()["frozen"] is True
        assert r.json()["current_version"] == 1

        # 冻结中 PATCH 被拦截，配置不被修改
        r2 = client.patch(
            f"/agent/{aid}", json={"system_prompt": "被篡改的提示词"},
        )
        assert r2.status_code == 403
        assert "已冻结" in r2.json()["detail"] and "v1" in r2.json()["detail"]
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[aid]["data"]["system_prompt"] == "你是测试专家"

    def test_frozen_patch_no_side_effects_on_type(self, stack):
        """冻结拦截在外层：PATCH 携带 agent_type 也不应落库（零副作用）。"""
        client, vs, db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        r = client.patch(
            f"/agent/{aid}", json={"agent_type": "leader"},
        )
        assert r.status_code == 403
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        # 类型仍是 member（PATCH 的 agent_type 未被 agent_type 中间件处理）
        assert agents[aid]["agent_type"] == "member"

    def test_unfreeze_allows_patch(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        r = client.post(f"/agent/{aid}/unfreeze", headers=U)
        assert r.status_code == 200 and r.json()["frozen"] is False

        r2 = client.patch(f"/agent/{aid}", json={"system_prompt": "开放模式迭代"})
        assert r2.status_code == 200
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[aid]["data"]["system_prompt"] == "开放模式迭代"

    def test_freeze_unfreeze_refreeze_same_content_same_version(self, stack):
        """冻结→解冻→未改内容再冻结：复用版本号，不膨胀。"""
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        client.post(f"/agent/{aid}/unfreeze", headers=U)
        r = client.post(f"/agent/{aid}/freeze", headers=U)
        assert r.json()["current_version"] == 1
        assert len(r.json()["versions"]) == 1

    def test_unfreeze_without_record_404(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        r = client.post(f"/agent/{aid}/unfreeze", headers=U)
        assert r.status_code == 404

    def test_freeze_nonexistent_agent_404(self, stack):
        client, _, _db = stack
        r = client.post("/agent/ghost/freeze", headers=U)
        assert r.status_code == 404

    def test_endpoints_require_login(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        assert client.post(f"/agent/{aid}/freeze").status_code == 401
        assert client.post(f"/agent/{aid}/unfreeze").status_code == 401
        assert client.post(f"/agent/{aid}/save-version").status_code == 401
        assert client.post(f"/agent/{aid}/versions/1/restore").status_code == 401


class TestSaveVersion:
    def test_save_version_keeps_editable(self, stack):
        """开放模式：迭代 → 点保存 → 新版本号；期间 PATCH 始终可用。"""
        client, _, _db = stack
        aid = _create_agent(client)
        r = client.post(f"/agent/{aid}/save-version", headers=U, json={"label": "v1"})
        assert r.json()["frozen"] is False
        assert r.json()["current_version"] == 1

        # 开放模式 PATCH 不受影响
        assert client.patch(
            f"/agent/{aid}", json={"system_prompt": "迭代第二稿"},
        ).status_code == 200

        r2 = client.post(f"/agent/{aid}/save-version", headers=U, json={"label": "v2"})
        assert r2.json()["current_version"] == 2
        assert len(r2.json()["versions"]) == 2
        # 快照内容与保存时点一致
        detail = client.get(f"/agent/{aid}/versions/2").json()
        assert detail["data"]["system_prompt"] == "迭代第二稿"
        assert detail["label"] == "v2"

    def test_save_version_force_always_increments(self, stack):
        """回归锁：显式发版（save-version）配置未变也必须新增版本。

        历史 bug：add_version 静默复用旧版本号 + label 不写入 →
        用户点「发布新版本」界面上毫无反应。
        """
        client, _, _ = stack
        aid = _create_agent(client)
        r1 = client.post(f"/agent/{aid}/save-version", headers=U, json={"label": ""})
        assert len(r1.json()["versions"]) == 1
        # 配置完全没变，再点一次发版
        r2 = client.post(
            f"/agent/{aid}/save-version", headers=U, json={"label": "重发"},
        )
        assert r2.json()["current_version"] == 2, "显式发版必须产生新版本号"
        assert len(r2.json()["versions"]) == 2
        assert r2.json()["versions"][1]["label"] == "重发", "label 必须写入"

    def test_freeze_keeps_dedup_on_identical_config(self, stack):
        """freeze 语义保留：配置未变的重复冻结不膨胀版本号。"""
        client, vs, _ = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        client.post(f"/agent/{aid}/unfreeze", headers=U)
        client.post(f"/agent/{aid}/freeze", headers=U)
        assert len(vs.record(aid)["versions"]) == 1, "freeze 复用同配置版本"


# ---------------------------------------------------------------- 复制

class TestDuplicate:
    """POST /agent/{id}/duplicate：从当前配置或版本快照分叉新个体。

    用户场景：高考志愿兵 v1 基础 → v2 文科优化 → v3 理科优化；
    复制 v1 再各自深入 = 建立第二、第三个志愿兵。
    """

    @pytest.fixture
    def dstack(self, env, monkeypatch):
        """复制专用最小栈：官方 CRUD + router + state.storage fake。"""
        from tests.official_contract import (
            agent_item,
            list_agent_response,
            post_agent_response,
        )

        inner = FastAPI()
        db = {"next": 1, "agents": {}, "owners": {}, "team_ids": set()}

        from app.agent_type import AgentTypeStore

        type_store = AgentTypeStore(str(env / "types.json"))

        @inner.post("/agent/")
        async def create(body: dict):
            aid = f"a{db['next']}"
            db["next"] += 1
            # 模拟 AgentTypeMiddleware：剥离 agent_type 存映射
            atype = body.pop("agent_type", None)
            db["agents"][aid] = body
            db["owners"][aid] = "u1"  # 与 U 常量身份一致
            if atype:
                type_store.set(aid, atype)
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

        inner.include_router(av.agent_version_router)

        async def fake_call(method, path, user_id, json_body=None, params=None):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t", timeout=10.0,
            ) as c:
                return await c.request(
                    method, path, json=json_body, params=params,
                    headers={"X-User-ID": user_id},
                )

        monkeypatch.setattr(av, "_call_official", fake_call)

        class _Rec:
            def __init__(self, source):
                self.source = source

        class _Storage:
            async def get_agent(self, user_id, agent_id):
                if db["owners"].get(agent_id) != user_id:
                    return None
                return _Rec(
                    "team" if agent_id in db["team_ids"] else "user",
                )

        inner.state.storage = _Storage()
        return TestClient(inner), db, env

    def test_duplicate_current_config(self, dstack):
        """默认复制当前配置：新个体同名副本、提示词一致、独立 id。"""
        client, db, _ = dstack
        aid = _create_agent(client, name="高考志愿兵", prompt="基础提示词")
        r = client.post(f"/agent/{aid}/duplicate", headers=U, json={})
        assert r.status_code == 200, r.text
        new_id = r.json()["agent_id"]
        assert new_id != aid
        assert r.json()["name"] == "高考志愿兵 副本"
        # 官方列表出现新个体，配置与源一致
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[new_id]["data"]["system_prompt"] == "基础提示词"

    def test_duplicate_from_version_snapshot(self, dstack):
        """复制历史版本：v1 文科 → v2 理科 → 复制 v1 得到文科分叉。"""
        client, _, _ = dstack
        aid = _create_agent(client, prompt="文科优化版")
        client.post(f"/agent/{aid}/save-version", headers=U)  # v1 = 文科
        client.patch(f"/agent/{aid}", json={"system_prompt": "理科优化版"})
        client.post(f"/agent/{aid}/save-version", headers=U)  # v2 = 理科

        r = client.post(
            f"/agent/{aid}/duplicate", headers=U,
            json={"name": "文科志愿兵", "version": 1},
        )
        assert r.status_code == 200, r.text
        new_id = r.json()["agent_id"]
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[new_id]["data"]["name"] == "文科志愿兵"
        assert agents[new_id]["data"]["system_prompt"] == "文科优化版"
        assert r.json()["source_version"] == 1

    def test_duplicate_unknown_version_404(self, dstack):
        client, _, _ = dstack
        aid = _create_agent(client)
        r = client.post(
            f"/agent/{aid}/duplicate", headers=U, json={"version": 9},
        )
        assert r.status_code == 404

    def test_duplicate_not_owner_404(self, dstack):
        """所有权校验走 get_agent 键控：别人的智能体（即使被共享）不可复制。"""
        client, db, _ = dstack
        aid = _create_agent(client)
        db["owners"][aid] = "someone-else"  # 换主
        r = client.post(f"/agent/{aid}/duplicate", headers=U, json={})
        assert r.status_code == 404

    def test_duplicate_team_worker_400(self, dstack):
        """团队成员（source=team）随团队生命周期，不可复制分叉。"""
        client, db, _ = dstack
        aid = _create_agent(client)
        db["team_ids"].add(aid)
        r = client.post(f"/agent/{aid}/duplicate", headers=U, json={})
        assert r.status_code == 400

    def test_duplicate_carries_agent_type(self, dstack):
        """类型跟随源：leader 复制出的新个体也是 leader。

        2026-09-09 回归锁：_call_official 绕过 AgentTypeMiddleware，
        body 带类型无人剥离（发布产品全被标默认小A → 无组队能力，
        图纸无法兑现）——必须创建成功后直写类型表，且 body 不带
        agent_type（官方 AgentData 无此字段）。
        """
        from app.agent_type import AgentTypeStore

        client, db, env = dstack
        aid = _create_agent(client)
        AgentTypeStore(str(env / "types.json")).set(aid, "leader")
        client.post(f"/agent/{aid}/duplicate", headers=U, json={})
        types = AgentTypeStore(str(env / "types.json")).load()
        new_ids = [i for i in types if i != aid]
        assert len(new_ids) == 1 and types[new_ids[0]] == "leader"
        # body 不携带 agent_type（官方无此字段，靠直写类型表）
        for stored in db["agents"].values():
            assert "agent_type" not in stored, (
                "POST body 仍带 agent_type——官方链路无人剥离，应直写类型表"
            )


class TestRestore:
    def test_restore_applies_old_config(self, stack):
        """开放模式迭代多版后恢复 v1：配置回到第一版。"""
        client, _, db = stack
        aid = _create_agent(client, prompt="第一版提示词")
        # v1 = 第一版
        client.post(f"/agent/{aid}/save-version", headers=U)
        client.patch(f"/agent/{aid}", json={"system_prompt": "第二版提示词"})
        client.post(f"/agent/{aid}/save-version", headers=U)  # v2 = 第二版

        r = client.post(f"/agent/{aid}/versions/1/restore", headers=U)
        assert r.status_code == 200, r.text
        assert r.json()["current_version"] == 1
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[aid]["data"]["system_prompt"] == "第一版提示词"

    def test_restore_while_frozen_is_authorized(self, stack):
        """冻结中恢复历史版本 = 显式授权操作，必须可执行（不经拦截链）。"""
        client, _, _db = stack
        aid = _create_agent(client, prompt="第一版提示词")
        client.post(f"/agent/{aid}/save-version", headers=U)  # v1
        client.patch(f"/agent/{aid}", json={"system_prompt": "第二版提示词"})
        client.post(f"/agent/{aid}/freeze", headers=U)  # v2 冻结

        r = client.post(f"/agent/{aid}/versions/1/restore", headers=U)
        assert r.status_code == 200, r.text
        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[aid]["data"]["system_prompt"] == "第一版提示词"
        # 冻结状态保持（恢复不等于解冻）
        assert agents[aid]["version"]["frozen"] is True

    def test_restore_nonexistent_version_404(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        r = client.post(f"/agent/{aid}/versions/99/restore", headers=U)
        assert r.status_code == 404


class TestInjectionAndCleanup:
    def test_get_list_injects_version_field(self, stack):
        """GET /agent/ 注入 version 状态，前端据此渲染冻结徽章。"""
        client, _, _db = stack
        aid_frozen = _create_agent(client, name="已冻结")
        aid_free = _create_agent(client, name="自由")
        client.post(f"/agent/{aid_frozen}/freeze", headers=U)

        agents = {a["id"]: a for a in client.get("/agent/").json()["agents"]}
        assert agents[aid_frozen]["version"] == {
            "frozen": True, "current_version": 1, "latest_version": 1,
        }
        assert agents[aid_free]["version"] == {
            "frozen": False, "current_version": None, "latest_version": None,
        }

    def test_versions_list_endpoint(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U, json={"label": "首版"})
        r = client.get(f"/agent/{aid}/versions")
        assert r.status_code == 200
        body = r.json()
        assert body["frozen"] is True
        assert [v["version"] for v in body["versions"]] == [1]
        assert body["versions"][0]["label"] == "首版"
        # 列表不含快照正文（详情接口才有）
        assert "data" not in body["versions"][0]

    def test_version_detail_contains_snapshot(self, stack):
        client, _, _db = stack
        aid = _create_agent(client, prompt="快照正文")
        client.post(f"/agent/{aid}/freeze", headers=U)
        r = client.get(f"/agent/{aid}/versions/1")
        assert r.status_code == 200
        assert r.json()["data"]["system_prompt"] == "快照正文"
        assert client.get(f"/agent/{aid}/versions/9").status_code == 404

    def test_delete_agent_cleans_versions(self, stack):
        client, vs, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        assert vs.record(aid)["frozen"] is True

        r = client.delete(f"/agent/{aid}")
        assert r.status_code == 200
        assert vs.record(aid) == {
            "frozen": False, "current_version": None, "versions": [],
        }

    def test_non_agent_paths_pass_through(self, stack):
        """非 /agent 路径与多段子路径（freeze 等路由）不受中间件影响。"""
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        # GET /agent/{id}/versions 是多段路径，透传到 router 正常工作
        assert client.get(f"/agent/{aid}/versions").status_code == 200


class TestEdgeCases:
    def test_freeze_label_stored(self, stack):
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U, json={"label": "对外服务版"})
        detail = client.get(f"/agent/{aid}/versions/1").json()
        assert detail["label"] == "对外服务版"

    def test_freeze_without_body_ok(self, stack):
        """POST 不带 body（label 可选）不应 422。"""
        client, _, _db = stack
        aid = _create_agent(client)
        r = client.post(f"/agent/{aid}/freeze", headers=U)
        assert r.status_code == 200, r.text

    def test_restore_write_failure_502(self, stack, monkeypatch):
        client, _, _db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)

        async def bad_call(method, path, user_id, json_body=None, params=None):
            return httpx.Response(500, json={})

        monkeypatch.setattr(av, "_call_official", bad_call)
        r = client.post(f"/agent/{aid}/versions/1/restore", headers=U)
        assert r.status_code == 502


# ================================================================ 团队图纸（方案 A）


class _BPData:
    """duck-typing 成员 agent 载荷（dataclass 形态）。"""

    def __init__(self, name, system_prompt, invite_description=None):
        self.name = name
        self.system_prompt = system_prompt
        self.invite_config = (
            {"invite_description": invite_description}
            if invite_description else None
        )


class _BPAgent:
    def __init__(self, agent_id, data):
        self.id = agent_id
        self.data = data


class _BPMember:
    def __init__(self, agent_id, session_id):
        self.agent_id = agent_id
        self.session_id = session_id


class _BPTeamData:
    def __init__(self, name, description, members):
        self.name = name
        self.description = description
        self.members = members


class _BPTeam:
    def __init__(self, team_id, session_id, leader_agent_id, data, updated_at=""):
        self.id = team_id
        self.session_id = session_id
        self.leader_agent_id = leader_agent_id
        self.data = data
        self.updated_at = updated_at


class _BPSession:
    def __init__(self, session_id, team_id=None):
        self.id = session_id
        self.team_id = team_id


class _BPStorage:
    """蓝图收集用最小 storage duck-typing。"""

    def __init__(self):
        self.teams = {}
        self.sessions = {}
        self.agents = {}

    async def list_teams(self, user_id):
        return list(self.teams.values())

    async def get_session(self, user_id, agent_id, session_id):
        return self.sessions.get(session_id)

    async def get_agent(self, user_id, agent_id):
        return self.agents.get(agent_id)


def _bp_storage_full(leader_agent="a-leader"):
    """完整团队：存活 leader 会话 + created/invited 两成员。"""
    st = _BPStorage()
    st.sessions["s-leader"] = _BPSession("s-leader", team_id="t-1")
    st.agents["a-m1"] = _BPAgent("a-m1", _BPData(
        "robot_dynamicist",
        "You are robot_dynamicist, a member of team '研究组' led by 大A.\n\n"
        "Team purpose: 双臂搬运研究\n\n"
        "Your role: 机器人动力学专家，负责建模\n\n"
        "You communicate with the team leader through TeamSay.",
    ))
    st.agents["a-m2"] = _BPAgent("a-m2", _BPData(
        "registry_expert",
        "You are registry_expert...\n\nYou communicate.",
        invite_description="注册表专家，负责领域检索",
    ))
    st.teams["t-1"] = _BPTeam(
        "t-1", "s-leader", leader_agent,
        _BPTeamData("双臂液体搬运研究组", "研究双臂搬运优化", [
            _BPMember("a-m1", "s-m1"),
            _BPMember("a-m2", "s-m2"),
        ]),
        updated_at="2026-09-09T00:00:00Z",
    )
    return st


class TestCollectTeamBlueprint:
    async def test_full_team_collected(self):
        """存活团队 → 团队名/宗旨 + 两成员定义（职责提取两种来源）。"""
        from app.agent_version import collect_team_blueprint
        st = _bp_storage_full()
        bp = await collect_team_blueprint("u1", "a-leader", st)
        assert bp["team_name"] == "双臂液体搬运研究组"
        assert bp["team_description"] == "研究双臂搬运优化"
        assert len(bp["members"]) == 2
        m1 = next(m for m in bp["members"] if m["name"] == "robot_dynamicist")
        # created 成员：Your role 段提取
        assert "动力学专家" in m1["description"]
        assert "机器人动力学专家，负责建模" == m1["description"]
        m2 = next(m for m in bp["members"] if m["name"] == "registry_expert")
        # invited 成员：invite_description 优先
        assert m2["description"] == "注册表专家，负责领域检索"
        # 完整提示词随快照留档
        assert "robot_dynamicist" in m1["system_prompt"]

    async def test_prefers_alive_team(self):
        """同时领导存活与已解散团队 → 选存活（leader 会话仍绑定）。"""
        from app.agent_version import collect_team_blueprint
        st = _bp_storage_full()
        # 旧的（已解散：会话 team_id=None）updated_at 更新
        st.teams["t-old"] = _BPTeam(
            "t-old", "s-old", "a-leader",
            _BPTeamData("旧团队", "", [_BPMember("a-m1", "s-m1")]),
            updated_at="2026-09-10T00:00:00Z",
        )
        st.sessions["s-old"] = _BPSession("s-old", team_id=None)
        bp = await collect_team_blueprint("u1", "a-leader", st)
        assert bp["team_name"] == "双臂液体搬运研究组"

    async def test_fallback_to_dissolved_when_no_alive(self):
        """无存活团队 → 退回最近领导的（软解散资产仍可发版）。"""
        from app.agent_version import collect_team_blueprint
        st = _bp_storage_full()
        st.sessions["s-leader"].team_id = None
        bp = await collect_team_blueprint("u1", "a-leader", st)
        assert bp["team_name"] == "双臂液体搬运研究组"

    async def test_no_team_none(self):
        """无在册团队 / 非该 agent 领导 → None。"""
        from app.agent_version import collect_team_blueprint
        st = _BPStorage()
        assert await collect_team_blueprint("u1", "a-leader", st) is None
        st2 = _bp_storage_full(leader_agent="someone-else")
        assert await collect_team_blueprint("u1", "a-leader", st2) is None

    async def test_missing_member_agent_skipped(self):
        """成员记录缺失（历史级联）→ 跳过，其余照常。"""
        from app.agent_version import collect_team_blueprint
        st = _bp_storage_full()
        del st.agents["a-m2"]
        bp = await collect_team_blueprint("u1", "a-leader", st)
        assert [m["name"] for m in bp["members"]] == ["robot_dynamicist"]


class TestBlueprintSnapshotFlow:
    """freeze → 快照含图纸 → duplicate 注入产品提示词。"""

    def test_freeze_embeds_blueprint(self, env, monkeypatch):
        from app.agent_version import AgentVersionStore
        st = _bp_storage_full(leader_agent="a1")
        client, vs, db = _make_stack(env, monkeypatch, storage=st)
        aid = _create_agent(client, name="机械控制实验室")
        r = client.post(f"/agent/{aid}/freeze", headers=U)
        assert r.status_code == 200, r.text
        entry = AgentVersionStore().get_version(aid, 1)
        bp = entry["data"]["team_blueprint"]
        assert bp["team_name"] == "双臂液体搬运研究组"
        assert {m["name"] for m in bp["members"]} == {
            "robot_dynamicist", "registry_expert",
        }

    def test_freeze_without_storage_no_blueprint(self, stack):
        """测试栈（无 state.storage）→ 快照无图纸字段，向后兼容。"""
        client, vs, db = stack
        aid = _create_agent(client)
        client.post(f"/agent/{aid}/freeze", headers=U)
        entry = vs.get_version(aid, 1)
        assert "team_blueprint" not in entry["data"]

    def test_dedup_counts_blueprint(self, env):
        """图纸变化 = 内容变化：不 force 时产生新版本号。"""
        vs = AgentVersionStore(str(env / "versions.json"))
        data = {"name": "大A", "system_prompt": "主理人"}
        vs.add_version("a1", data, "v1", team_blueprint={"team_name": "T1", "members": [{"name": "m1"}]})
        # 同内容同图纸 → 复用 v1
        e2 = vs.add_version("a1", data, "", team_blueprint={"team_name": "T1", "members": [{"name": "m1"}]})
        assert e2["version"] == 1
        # 图纸变了 → v2
        e3 = vs.add_version("a1", data, "", team_blueprint={"team_name": "T2", "members": [{"name": "m1"}]})
        assert e3["version"] == 2

    def test_duplicate_injects_blueprint_prompt(self, stack):
        """发布/复制：快照图纸 → 产品 system_prompt 含重建指令，
        且 payload 不带 team_blueprint 字段（官方 API 不认）。"""
        import asyncio
        from app.agent_version import AgentVersionStore, duplicate_agent_core
        client, vs, db = stack
        aid = _create_agent(client, name="机械控制实验室")
        vs.add_version(
            aid,
            {"name": "机械控制实验室", "system_prompt": "你是主理人"},
            "发版",
            team_blueprint={
                "team_name": "双臂液体搬运研究组",
                "team_description": "研究双臂搬运优化",
                "members": [
                    {"name": "robot_dynamicist",
                     "description": "机器人动力学专家，负责建模",
                     "system_prompt": "full..."},
                    {"name": "registry_expert",
                     "description": "注册表专家，负责领域检索",
                     "system_prompt": "full2..."},
                ],
            },
        )
        class _OwnerStorage:
            """所有权校验 stub：任何 agent 都属于 u1、非 team 成员。"""

            def __init__(self, agent_id):
                self._aid = agent_id

            async def get_agent(self, user_id, agent_id):
                class _R:
                    source = "user"
                return _R() if agent_id == self._aid else None

        storage = _OwnerStorage(aid)
        dup = asyncio.run(
            duplicate_agent_core(
                aid, "u1", "机械臂控制", 1, storage,
            ),
        )
        new_id = dup["agent_id"]
        created = db["agents"][new_id]
        sp = created["system_prompt"]
        # 注入图纸章节：团队名 + 成员名 + 职责 + 重建指令
        assert "双臂液体搬运研究组" in sp
        assert "robot_dynamicist" in sp and "registry_expert" in sp
        assert "机器人动力学专家，负责建模" in sp
        assert "AgentCreate" in sp
        # 字段剥离：官方 payload 不带图纸
        assert "team_blueprint" not in created
        # 原提示词保留
        assert sp.startswith("你是主理人")

    def test_duplicate_without_blueprint_untouched(self, stack):
        """旧版本快照（无图纸）→ 复制行为不变。"""
        import asyncio
        from app.agent_version import duplicate_agent_core
        client, vs, db = stack
        aid = _create_agent(client, name="普通智能体", prompt="原始提示词")
        vs.add_version(aid, {"name": "普通智能体", "system_prompt": "原始提示词"})
        class _OwnerStorage:
            async def get_agent(self, user_id, agent_id):
                class _R:
                    source = "user"
                return _R() if agent_id == aid else None

        dup = asyncio.run(
            duplicate_agent_core(aid, "u1", "普通智能体 副本", 1, _OwnerStorage()),
        )
        created = db["agents"][dup["agent_id"]]
        assert created["system_prompt"] == "原始提示词"

    def test_duplicate_auto_mode_skips_blueprint(self, stack):
        """team_mode=auto（自动组建）：不注入名单，保留原始提示词。

        发布"不带团队的主理人"——产品仍有 leader 组队能力，按任务
        即兴 AgentCreate（2026-09-09 发布形态二分）。
        """
        import asyncio
        from app.agent_version import duplicate_agent_core
        client, vs, db = stack
        aid = _create_agent(client, name="机械控制实验室", prompt="你是主理人")
        vs.add_version(
            aid,
            {"name": "机械控制实验室", "system_prompt": "你是主理人"},
            "带图纸版本",
            team_blueprint={
                "team_name": "双臂液体搬运研究组",
                "members": [{"name": "robot_dynamicist",
                             "description": "机器人动力学专家",
                             "system_prompt": "full..."}],
            },
        )

        class _OwnerStorage:
            async def get_agent(self, user_id, agent_id):
                class _R:
                    source = "user"
                return _R() if agent_id == aid else None

        dup = asyncio.run(
            duplicate_agent_core(
                aid, "u1", "自动组建版", 1, _OwnerStorage(),
                team_mode="auto",
            ),
        )
        created = db["agents"][dup["agent_id"]]
        # 未注入任何图纸痕迹
        assert created["system_prompt"] == "你是主理人"
        assert "双臂液体搬运研究组" not in created["system_prompt"]
        assert "robot_dynamicist" not in created["system_prompt"]
        assert "team_blueprint" not in created

    def test_restore_strips_blueprint(self, stack, monkeypatch):
        """恢复版本：PATCH body 剥离图纸字段（官方 AgentData 无此字段）。"""
        client, vs, db = stack
        aid = _create_agent(client)
        vs.add_version(
            aid, {"name": "大A", "system_prompt": "主理人提示词"}, "v1",
            team_blueprint={"team_name": "T", "members": [{"name": "m"}]},
        )
        # 不经 fake_call_official 而是直接验证：restore 用 _call_official
        r = client.post(f"/agent/{aid}/versions/1/restore", headers=U)
        assert r.status_code == 200, r.text
        # db 中 agent 更新后的内容（PATCH body 经 fake_call_official 进 db）
        assert db["agents"][aid]["system_prompt"] == "主理人提示词"
        assert "team_blueprint" not in db["agents"][aid]
