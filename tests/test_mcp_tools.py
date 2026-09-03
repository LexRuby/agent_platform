"""MCP 工具清单查看 API 测试：GET /mcp-tools/{id}。

不访问真实 MCP server：monkeypatch ``_list_tools_via_probe``；
storage 用假实现（get_mcp 按 id 返回预置 record 或 None）。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import mcp_tools
from app.mcp_tools import MCPToolView, mcp_tools_router


class FakeRecord:
    """最小 MCPRecord 形状：client.name + display_name + description。"""

    def __init__(self, name, display_name=None, description=""):
        class C:
            pass

        self.client = C()
        self.client.name = name
        self.display_name = display_name
        self.description = description


class FakeStorage:
    def __init__(self, records):
        self._records = records

    async def get_mcp(self, user_id, mcp_id):
        return self._records.get(mcp_id)


@pytest.fixture
def client_factory(monkeypatch):
    """返回 (records, probe_result, probe_exc) → TestClient 的工厂。

    probe 可控：probe_result 为 _list_tools_via_probe 的返回值；
    probe_exc 非 None 时抛出该异常（模拟 MCP server 连不上）。
    """
    made = {}

    async def fake_probe(client):
        if made.get("exc") is not None:
            raise made["exc"]
        return made.get("result", [])

    monkeypatch.setattr(mcp_tools, "_list_tools_via_probe", fake_probe)

    def make(records, probe_result=None, probe_exc=None):
        made["result"] = probe_result
        made["exc"] = probe_exc
        app = FastAPI()
        app.include_router(mcp_tools_router)
        mcp_tools.init_mcp_tools(FakeStorage(records))
        return TestClient(app)

    return make


def _auth(client):
    return client.get(
        "/mcp-tools/some-id", headers={"X-User-ID": "alice"},
    )


class TestGetMCPTools:
    def test_returns_tools_with_metadata(self, client_factory):
        c = client_factory(
            {"some-id": FakeRecord(
                "smart-writing", "智能写作", "写作能力 MCP",
            )},
            probe_result=[
                MCPToolView(
                    name="detect_word_ontology",
                    description="术语本体识别",
                    input_schema={"type": "object", "required": ["query"]},
                ),
                MCPToolView(name="user_results", description="结果库"),
            ],
        )
        r = _auth(c)
        assert r.status_code == 200
        body = r.json()
        assert body["server"] == "smart-writing"
        assert body["display_name"] == "智能写作"
        assert body["description"] == "写作能力 MCP"
        assert len(body["tools"]) == 2
        assert body["tools"][0]["name"] == "detect_word_ontology"
        assert body["tools"][0]["input_schema"]["required"] == ["query"]
        # 无 schema 的工具 input_schema 为 null（JSON 序列化为 null）
        assert body["tools"][1]["input_schema"] is None

    def test_unknown_mcp_404(self, client_factory):
        c = client_factory({})
        assert _auth(c).status_code == 404

    def test_probe_failure_502(self, client_factory):
        c = client_factory(
            {"some-id": FakeRecord("x")},
            probe_exc=TimeoutError("connect timeout"),
        )
        r = _auth(c)
        assert r.status_code == 502
        assert "无法连接" in r.json()["detail"]

    def test_missing_user_header_422(self, client_factory):
        c = client_factory({"some-id": FakeRecord("x")})
        # X-User-ID 必填：缺头 422
        assert c.get("/mcp-tools/some-id").status_code == 422

    def test_storage_uninitialized_503(self, monkeypatch):
        # init 未调用时明确 503，而不是 AttributeError 500
        monkeypatch.setattr(mcp_tools, "_storage", None)
        app = FastAPI()
        app.include_router(mcp_tools_router)
        c = TestClient(app)
        assert _auth(c).status_code == 503


class TestListToolsViaProbe:
    """_list_tools_via_probe：http 走 stateless 副本，stdio 走临时连接。"""

    @pytest.mark.parametrize(
        "config_type,is_stateful,expect_probe_stateful", [
            # http_mcp：无论 record 是不是 stateful，probe 都是 stateless
            ("http_mcp", True, False),
            ("http_mcp", False, False),
            # stdio_mcp：必须 stateful
            ("stdio_mcp", True, True),
        ],
    )
    async def test_probe_statefulness_by_config_type(
        self, monkeypatch, config_type, is_stateful, expect_probe_stateful,
    ):
        from agentscope.mcp import MCPClient
        from agentscope.mcp._config import HttpMCPConfig, StdioMCPConfig
        from app import mcp_tools as mod

        config = (
            HttpMCPConfig(url="http://127.0.0.1:1/mcp")
            if config_type == "http_mcp"
            else StdioMCPConfig(command="true")
        )
        client = MCPClient(
            name="probe-target", is_stateful=is_stateful,
            mcp_config=config,
        )

        seen = {}

        class FakeProbe:
            def __init__(self, **kw):
                seen.update(kw)
                self._raw = [type("T", (), {
                    "name": "t1", "description": "d1", "inputSchema": {},
                })()]

            async def list_raw_tools(self):
                return self._raw

            async def connect(self):
                pass

            async def close(self):
                pass

        monkeypatch.setattr(mod, "MCPClient", FakeProbe)
        tools = await mod._list_tools_via_probe(client)
        # probe 按预期选择了 stateful / stateless
        assert seen["is_stateful"] is expect_probe_stateful
        assert tools[0].name == "t1"
        assert tools[0].description == "d1"
