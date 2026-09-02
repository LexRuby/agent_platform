"""控制台 API（/api）测试：MCP 注册流程、Agent 编排、工作空间、发布大厅。

模型调用走 fake_llm 模式（不访问真实 LLM）；MCP 拉取用 monkeypatch 模拟。
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app import console, mcp_client
from app.registry import AGT_DRAFT, AGT_LOCKED, AGT_PUBLISHED, Registry
from app.settings import Settings

FAKE_TOOLS = [
    {"name": "search", "description": "检索", "input_schema": {"type": "object"}},
    {"name": "write", "description": "写作", "input_schema": {"type": "object"}},
]


def make_settings(tmp_path, **over) -> Settings:
    base = dict(
        llm_provider="doubao", model_name="test-model",
        dashscope_api_key="", ark_api_key="k", ark_base_url="http://ark",
        midplatform_base_url="http://mid", midplatform_token="",
        studio_url="", store_path=str(tmp_path / "tasks.json"),
        templates_dir=str(tmp_path / "tpl"), fake_llm=True,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """注入临时 settings + registry 的控制台 API。"""
    settings = make_settings(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    console._init(settings, registry)
    monkeypatch.setattr(console, "_sessions", {})  # 测试间清空会话缓存

    app = FastAPI()
    app.include_router(console.router)
    return TestClient(app)


def make_agent(api, name="助手", tools=None):
    return api.post(
        "/api/agents",
        json={"name": name, "description": "d", "system_prompt": "sp",
              "tools": tools or []},
    )


class TestToolsEndpoint:
    def test_builtin_tools_listed(self, api):
        tools = api.get("/api/tools").json()
        refs = [t["ref"] for t in tools]
        assert all(r.startswith("builtin:") for r in refs)
        assert len(refs) > 0

    def test_mcp_tools_merged_after_registration(self, api, monkeypatch):
        async def fake_list_tools(url, token=None):
            return FAKE_TOOLS

        monkeypatch.setattr(mcp_client, "list_tools", fake_list_tools)
        monkeypatch.setattr(console.mcp_client, "list_tools", fake_list_tools)
        resp = api.post(
            "/api/mcp/servers",
            json={"name": "srv", "url": "http://x/mcp"},
        )
        assert resp.status_code == 200
        assert resp.json()["tools_count"] == 2

        tools = api.get("/api/tools").json()
        refs = [t["ref"] for t in tools]
        assert "mcp:srv:search" in refs
        assert "mcp:srv:write" in refs


class TestMcpRegistration:
    def test_register_failure_rolls_back(self, api, monkeypatch):
        """注册时连不上 MCP → 502 且不留脏数据。"""
        async def broken(url, token=None):
            raise mcp_client.McpError("connection refused")

        monkeypatch.setattr(console.mcp_client, "list_tools", broken)
        resp = api.post(
            "/api/mcp/servers", json={"name": "srv", "url": "http://bad"},
        )
        assert resp.status_code == 502
        assert "MCP 连接失败" in resp.json()["detail"]
        assert api.get("/api/mcp/servers").json() == []

    def test_refresh_unknown_404(self, api):
        assert api.post("/api/mcp/servers/ghost/refresh").status_code == 404

    def test_refresh_updates_tools(self, api, monkeypatch):
        async def fake(url, token=None):
            return FAKE_TOOLS

        monkeypatch.setattr(console.mcp_client, "list_tools", fake)
        api.post("/api/mcp/servers", json={"name": "srv", "url": "http://x"})
        resp = api.post("/api/mcp/servers/srv/refresh")
        assert resp.status_code == 200
        assert resp.json()["tools_count"] == 2

    def test_delete(self, api, monkeypatch):
        async def fake(url, token=None):
            return FAKE_TOOLS

        monkeypatch.setattr(console.mcp_client, "list_tools", fake)
        api.post("/api/mcp/servers", json={"name": "srv", "url": "http://x"})
        assert api.delete("/api/mcp/servers/srv").status_code == 200
        assert api.get("/api/mcp/servers").json() == []

    def test_list_never_exposes_token(self, api, monkeypatch):
        async def fake(url, token=None):
            return FAKE_TOOLS

        monkeypatch.setattr(console.mcp_client, "list_tools", fake)
        api.post(
            "/api/mcp/servers",
            json={"name": "srv", "url": "http://x", "token": "hush"},
        )
        body = str(api.get("/api/mcp/servers").json())
        assert "hush" not in body


class TestAgentEndpoints:
    def test_create_and_get(self, api):
        resp = make_agent(api, "顾问", tools=["builtin:t"])
        assert resp.status_code == 200
        aid = resp.json()["id"]
        got = api.get(f"/api/agents/{aid}")
        assert got.json()["name"] == "顾问"
        assert got.json()["tools"] == ["builtin:t"]

    def test_get_missing_404(self, api):
        assert api.get("/api/agents/nope").status_code == 404

    def test_update_draft_ok(self, api):
        aid = make_agent(api).json()["id"]
        resp = api.put(
            f"/api/agents/{aid}",
            json={"name": "改名", "description": "d",
                  "system_prompt": "sp2", "tools": []},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "改名"

    def test_update_locked_409(self, api):
        aid = make_agent(api).json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        resp = api.put(
            f"/api/agents/{aid}",
            json={"name": "x", "description": "d", "system_prompt": "s",
                  "tools": []},
        )
        assert resp.status_code == 409

    def test_lock_requires_draft(self, api):
        aid = make_agent(api).json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        assert api.post(f"/api/agents/{aid}/lock").status_code == 409

    def test_unlock_restores_draft(self, api):
        aid = make_agent(api).json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        resp = api.post(f"/api/agents/{aid}/unlock")
        assert resp.json()["status"] == AGT_DRAFT

    def test_publish_requires_locked(self, api):
        """draft 直接发布 → 409（必须先锁定定稿）。"""
        aid = make_agent(api).json()["id"]
        assert api.post(f"/api/agents/{aid}/publish").status_code == 409
        api.post(f"/api/agents/{aid}/lock")
        assert api.post(f"/api/agents/{aid}/publish").status_code == 200
        assert api.get(f"/api/agents/{aid}").json()["status"] == AGT_PUBLISHED

    def test_lock_missing_404(self, api):
        assert api.post("/api/agents/nope/lock").status_code == 404


class TestWorkspace:
    def test_chat_persists_history(self, api):
        aid = make_agent(api).json()["id"]
        resp = api.post(f"/api/agents/{aid}/chat", json={"message": "你好"})
        assert resp.status_code == 200
        assert "[demo]" in resp.json()["reply"]
        ws = api.get(f"/api/agents/{aid}/workspace").json()
        assert [m["role"] for m in ws["messages"]] == ["user", "assistant"]
        assert ws["messages"][0]["content"] == "你好"

    def test_chat_locked_agent_409(self, api):
        """锁定定稿后，打磨对话必须被拒绝。"""
        aid = make_agent(api).json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        resp = api.post(f"/api/agents/{aid}/chat", json={"message": "hi"})
        assert resp.status_code == 409
        assert "draft" in resp.json()["detail"]

    def test_chat_missing_agent_404(self, api):
        resp = api.post("/api/agents/nope/chat", json={"message": "hi"})
        assert resp.status_code == 404

    def test_workspace_missing_agent_404(self, api):
        assert api.get("/api/agents/nope/workspace").status_code == 404

    def test_clear_workspace(self, api):
        aid = make_agent(api).json()["id"]
        api.post(f"/api/agents/{aid}/chat", json={"message": "hi"})
        assert api.delete(f"/api/agents/{aid}/workspace").status_code == 200
        assert api.get(f"/api/agents/{aid}/workspace").json()["messages"] == []

    def test_fake_reply_mentions_tool_count(self, api):
        aid = make_agent(api, tools=["builtin:a", "builtin:b"]).json()["id"]
        reply = api.post(
            f"/api/agents/{aid}/chat", json={"message": "hi"},
        ).json()["reply"]
        assert "2" in reply  # 装配工具数


class TestPublishedHall:
    def test_only_published_listed(self, api):
        draft = make_agent(api, "draft-one").json()["id"]
        locked = make_agent(api, "locked-one").json()["id"]
        pub = make_agent(api, "pub-one").json()["id"]
        api.post(f"/api/agents/{locked}/lock")
        api.post(f"/api/agents/{pub}/lock")
        api.post(f"/api/agents/{pub}/publish")

        names = [a["name"] for a in api.get("/api/published").json()]
        assert names == ["pub-one"]

    def test_published_chat_ok(self, api):
        aid = make_agent(api, "pub").json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        api.post(f"/api/agents/{aid}/publish")
        resp = api.post(f"/api/published/{aid}/chat", json={"message": "你好"})
        assert resp.status_code == 200
        assert "[demo]" in resp.json()["reply"]

    def test_published_chat_unpublished_404(self, api):
        """未发布的 Agent 不能通过对外入口访问（draft/locked 均 404）。"""
        draft = make_agent(api, "d").json()["id"]
        locked = make_agent(api, "l").json()["id"]
        api.post(f"/api/agents/{locked}/lock")
        assert api.post(f"/api/published/{draft}/chat",
                        json={"message": "x"}).status_code == 404
        assert api.post(f"/api/published/{locked}/chat",
                        json={"message": "x"}).status_code == 404

    def test_published_chat_missing_404(self, api):
        assert api.post("/api/published/nope/chat",
                        json={"message": "x"}).status_code == 404

    def test_published_chat_independent_of_workspace(self, api):
        """对外会话不写入打磨工作空间历史。"""
        aid = make_agent(api, "pub").json()["id"]
        api.post(f"/api/agents/{aid}/lock")
        api.post(f"/api/agents/{aid}/publish")
        api.post(f"/api/published/{aid}/chat", json={"message": "外部问题"})
        ws = api.get(f"/api/agents/{aid}/workspace").json()
        assert ws["messages"] == []
