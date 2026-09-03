"""agent_type 模块测试：大A/小A 分类的存储与中间件行为。

测试隔离原则：
- 映射文件 → tmp_path（不读写 data/agent_types.json）
- HTTP → starlette TestClient（ASGI 进程内调用，模拟官方 agent API）
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent_type import (
    LEADER,
    MEMBER,
    AgentTypeMiddleware,
    AgentTypeStore,
    _extract_agent_type,
    _match,
    agent_type_schema_property,
)


@pytest.fixture
def store(tmp_path):
    return AgentTypeStore(tmp_path / "types.json")


@pytest.fixture
def client(store):
    """模拟官方 agent API（路由与真实服务一致）。

    内层 mock 的响应结构一律取自 tests/official_contract.py（真实契约），
    禁止手写内联结构——曾因 mock 返回 {"id": ...} 与真实 agent_id 不符，
    POST 建立的类型映射在线上从未生效而测试全绿。
    """
    from tests.official_contract import (
        agent_item,
        list_agent_response,
        post_agent_response,
    )

    inner = FastAPI()
    db = {"next": 1, "agents": {}}  # 内存伪存储

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

    app = AgentTypeMiddleware(inner, store=store)
    return TestClient(app)


class TestStore:
    """AgentTypeStore 文件持久化。"""

    def test_default_member(self, store):
        assert store.get("unknown") == MEMBER

    def test_set_and_get(self, store):
        store.set("a1", LEADER)
        assert store.get("a1") == LEADER
        assert store.load() == {"a1": LEADER}

    def test_set_invalid_raises(self, store):
        with pytest.raises(ValueError):
            store.set("a1", "bogus")

    def test_delete(self, store):
        store.set("a1", LEADER)
        store.delete("a1")
        assert store.get("a1") == MEMBER

    def test_delete_absent_is_noop(self, store, tmp_path):
        store.delete("nope")
        assert not (tmp_path / "types.json").exists()

    def test_set_same_value_skips_write(self, store, tmp_path):
        store.set("a1", LEADER)
        mtime = (tmp_path / "types.json").stat().st_mtime_ns
        store.set("a1", LEADER)
        assert (tmp_path / "types.json").stat().st_mtime_ns == mtime

    def test_corrupted_file_degrades_to_empty(self, tmp_path):
        f = tmp_path / "types.json"
        f.write_text("{not json", encoding="utf-8")
        store = AgentTypeStore(f)
        assert store.load() == {}

    def test_invalid_values_in_file_are_filtered(self, tmp_path):
        f = tmp_path / "types.json"
        f.write_text(json.dumps({"a1": "leader", "a2": "bogus"}), encoding="utf-8")
        store = AgentTypeStore(f)
        assert store.load() == {"a1": "leader"}

    def test_missing_file_returns_empty(self, tmp_path):
        store = AgentTypeStore(tmp_path / "nope.json")
        assert store.load() == {}

    def test_env_var_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTFORGE_AGENT_TYPES_FILE", str(tmp_path / "env.json"))
        store = AgentTypeStore()
        store.set("a1", LEADER)
        assert (tmp_path / "env.json").exists()


class TestHelpers:
    """_match / _extract_agent_type / schema 属性。"""

    def test_match_collection_paths(self):
        # 官方集合路径带尾斜杠，必须两种都匹配
        assert _match("/agent", "POST") == {"id": None}
        assert _match("/agent/", "POST") == {"id": None}
        assert _match("/agent/", "GET") == {"id": None}

    def test_match_item_paths(self):
        assert _match("/agent/a1", "PATCH") == {"id": "a1"}
        assert _match("/agent/a1", "DELETE") == {"id": "a1"}
        assert _match("/agent/a1/", "PATCH") == {"id": "a1"}  # 尾斜杠容忍

    def test_match_rejects(self):
        assert _match("/agent", "DELETE") == {}  # 集合路径不允许 DELETE
        assert _match("/agent/a1/sub", "PATCH") == {}  # 多级不匹配
        assert _match("/agents", "POST") == {}  # 前缀不同
        assert _match("/sessions/", "GET") == {}

    def test_extract_strips_agent_type(self):
        body, at = _extract_agent_type(b'{"name":"x","agent_type":"leader"}')
        assert at == "leader"
        assert json.loads(body) == {"name": "x"}

    def test_extract_keeps_body_without_key(self):
        raw = b'{"name":"x"}'
        body, at = _extract_agent_type(raw)
        assert at is None
        assert body == raw

    def test_extract_passthrough_non_json(self):
        raw = b"not-json"
        body, at = _extract_agent_type(raw)
        assert at is None
        assert body == raw

    def test_schema_property_shape(self):
        p = agent_type_schema_property()
        assert p["enum"] == ["leader", "member"]
        assert p["default"] == "member"
        assert p["type"] == "string"


class TestMiddlewareWrite:
    """POST / PATCH / DELETE 的捕获与清理。"""

    def test_post_strips_and_stores(self, client, store):
        r = client.post("/agent/", json={
            "name": "主理人", "agent_type": "leader",
        })
        assert r.status_code == 200
        aid = r.json()["agent_id"]
        # 剥离：内层（官方）收到的 body 无 agent_type（经 GET 列表回读验证）
        listed = client.get("/agent/").json()["agents"]
        assert "agent_type" not in listed[0]["data"]
        # 存储：响应中的 agent_id 建立映射（真实结构：顶层 agent_id）
        assert store.get(aid) == LEADER
        # 列表注入的 agent_type 同步生效
        assert listed[0]["agent_type"] == LEADER

    def test_post_without_agent_type_no_mapping(self, client, store):
        r = client.post("/agent/", json={"name": "普通"})
        assert store.get(r.json()["agent_id"]) == MEMBER
        assert store.load() == {}  # 未写文件

    def test_patch_stores_from_path(self, client, store):
        aid = client.post("/agent/", json={"name": "x"}).json()["agent_id"]
        r = client.patch(f"/agent/{aid}", json={"agent_type": "leader"})
        assert r.status_code == 200
        assert "agent_type" not in r.json()["data"]
        assert store.get(aid) == LEADER

    def test_patch_invalid_value_ignored(self, client, store):
        aid = client.post("/agent/", json={"name": "x"}).json()["agent_id"]
        r = client.patch(f"/agent/{aid}", json={"agent_type": "bogus"})
        assert r.status_code == 200
        assert store.get(aid) == MEMBER

    def test_delete_cleans_mapping(self, client, store):
        aid = client.post(
            "/agent/", json={"name": "x", "agent_type": "leader"},
        ).json()["agent_id"]
        assert store.get(aid) == LEADER
        r = client.delete(f"/agent/{aid}")
        assert r.status_code == 200
        assert store.get(aid) == MEMBER

    def test_delete_nonexistent_agent_still_ok(self, client, store):
        store.set("ghost", LEADER)
        r = client.delete("/agent/ghost")
        assert r.status_code == 200
        assert store.get("ghost") == MEMBER


class TestMiddlewareRead:
    """GET /agent/ 列表注入。"""

    def test_list_injects_agent_type(self, client):
        client.post("/agent/", json={"name": "主理人", "agent_type": "leader"})
        client.post("/agent/", json={"name": "小A"})
        agents = client.get("/agent/").json()["agents"]
        by_name = {a["data"]["name"]: a for a in agents}
        assert by_name["主理人"]["agent_type"] == LEADER
        assert by_name["小A"]["agent_type"] == MEMBER

    def test_empty_list_ok(self, client):
        agents = client.get("/agent/").json()["agents"]
        assert agents == []

    def test_non_agent_paths_passthrough(self, client):
        r = client.get("/nonexistent")
        assert r.status_code == 404

    async def test_non_http_scope_passthrough(self, store):
        called = []

        async def inner(scope, receive, send):
            called.append(scope["type"])

        app = AgentTypeMiddleware(inner, store=store)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        await app({"type": "lifespan"}, receive, send)
        assert called == ["lifespan"]
