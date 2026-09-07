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


def _make_stack(env, monkeypatch):
    """与生产同构的中间件链：Version(LeaderTeam(AgentType(官方)))。

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
        """类型跟随源：leader 复制出的新个体也是 leader。"""
        from app.agent_type import AgentTypeStore

        client, _, env = dstack
        aid = _create_agent(client)
        AgentTypeStore(str(env / "types.json")).set(aid, "leader")
        client.post(f"/agent/{aid}/duplicate", headers=U, json={})
        types = AgentTypeStore(str(env / "types.json")).load()
        new_ids = [i for i in types if i != aid]
        assert len(new_ids) == 1 and types[new_ids[0]] == "leader"


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
