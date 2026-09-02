"""MCP 客户端测试：JSON-RPC 协议、SSE 响应解析、会话握手、错误路径。

HTTP 层用 httpx.MockTransport 模拟远端 MCP Server。
"""

import json

import httpx
import pytest

from app import mcp_client
from app.mcp_client import McpError, _parse_response


def sse_response(*messages, session_id=None) -> httpx.Response:
    """构造 text/event-stream 形式的 MCP 响应。"""
    lines = []
    for m in messages:
        if m is not None:
            lines.append(f"data: {json.dumps(m)}")
            lines.append("")
    headers = {"content-type": "text/event-stream"}
    if session_id:
        headers["mcp-session-id"] = session_id
    return httpx.Response(200, text="\n".join(lines), headers=headers)


def patch_transport(monkeypatch, handler):
    """替换 mcp_client._rpc 内新建的 AsyncClient。"""
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", PatchedClient)


class TestParseResponse:
    def test_plain_json(self):
        resp = httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}},
        )
        assert _parse_response(resp)["id"] == 1

    def test_sse_with_id(self):
        resp = sse_response(
            {"jsonrpc": "2.0", "method": "notify"},  # 无 id，跳过
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        )
        assert _parse_response(resp)["result"] == {"ok": True}

    def test_sse_without_id_raises(self):
        resp = sse_response({"jsonrpc": "2.0", "method": "progress"})
        with pytest.raises(McpError, match="未找到"):
            _parse_response(resp)


class TestRpc:
    async def test_jsonrpc_envelope(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"},
            )

        patch_transport(monkeypatch, handler)
        result, _ = await mcp_client._rpc("http://x/mcp", "tools/list")
        assert result == "ok"
        assert seen["body"]["jsonrpc"] == "2.0"
        assert seen["body"]["method"] == "tools/list"
        assert seen["body"]["id"] == 1
        # Accept 头兼容两种响应形式
        assert "application/json" in seen["headers"]["accept"]
        assert "text/event-stream" in seen["headers"]["accept"]

    async def test_error_status_raises(self, monkeypatch):
        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(500, text="server error"),
        )
        with pytest.raises(McpError, match="500"):
            await mcp_client._rpc("http://x/mcp", "tools/list")

    async def test_jsonrpc_error_raises(self, monkeypatch):
        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32601, "message": "no method"}},
            ),
        )
        with pytest.raises(McpError, match="no method"):
            await mcp_client._rpc("http://x/mcp", "tools/unknown")

    async def test_bearer_token_sent(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {}})

        patch_transport(monkeypatch, handler)
        await mcp_client._rpc("http://x/mcp", "ping", token="tok123")
        assert seen["auth"] == "Bearer tok123"


class TestConnect:
    async def test_initialize_handshake(self, monkeypatch):
        seen = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body["method"])
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": 1,
                          "result": {"protocolVersion": "2025-03-26"}},
                    headers={"mcp-session-id": "sess-1"},
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {}})

        patch_transport(monkeypatch, handler)
        sid = await mcp_client.connect("http://x/mcp")
        assert sid == "sess-1"
        assert seen[0] == "initialize"
        assert seen[1] == "notifications/initialized"  # 握手完成通知

    async def test_no_session_means_no_initialized_notification(
        self, monkeypatch,
    ):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content)["method"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {}})

        patch_transport(monkeypatch, handler)
        assert await mcp_client.connect("http://x/mcp") == ""
        assert seen == ["initialize"]


class TestListTools:
    async def test_full_flow(self, monkeypatch):
        """connect → tools/list，工具字段映射为内部格式。"""

        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": {}},
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"tools": [
                        {"name": "search",
                         "description": "检索",
                         "inputSchema": {"type": "object"}},
                        {"name": "bare"},  # 缺 schema/description 的容错
                    ]},
                },
            )

        patch_transport(monkeypatch, handler)
        tools = await mcp_client.list_tools("http://x/mcp")
        assert tools[0] == {
            "name": "search", "description": "检索",
            "input_schema": {"type": "object"},
        }
        assert tools[1]["description"] == ""
        assert tools[1]["input_schema"] == {"type": "object", "properties": {}}


class TestCallTool:
    async def test_text_content_joined(self, monkeypatch):
        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": {}})
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "search"
            assert body["params"]["arguments"] == {"q": "x"}
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "image", "data": "..."},  # 非文本块忽略
                        {"type": "text", "text": "第二段"},
                    ]},
                },
            )

        patch_transport(monkeypatch, handler)
        out = await mcp_client.call_tool(
            "http://x/mcp", "search", {"q": "x"},
        )
        assert out == "第一段\n第二段"

    async def test_tool_error_raises(self, monkeypatch):
        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": {}})
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "result": {"isError": True, "content": []}},
            )

        patch_transport(monkeypatch, handler)
        with pytest.raises(McpError, match="工具执行出错"):
            await mcp_client.call_tool("http://x/mcp", "t", {})

    async def test_null_result_returns_empty(self, monkeypatch):
        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": {}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": None})

        patch_transport(monkeypatch, handler)
        assert await mcp_client.call_tool("http://x/mcp", "t", {}) == ""
