"""极简 MCP 客户端（Streamable HTTP 传输，JSON-RPC 2.0）。

支持注册远端 MCP Server（streamable http 端点），拉取工具清单、调用工具。
协议版本: 2025-03-26。响应兼容 application/json 与 text/event-stream 两种形式。
"""

import json

import httpx

PROTOCOL_VERSION = "2025-03-26"
TIMEOUT = 60.0


class McpError(RuntimeError):
    pass


def _headers(session_id: str | None, token: str | None) -> dict:
    headers = {"Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_response(resp: httpx.Response) -> dict:
    """MCP streamable http 响应可能是纯 JSON，也可能是 SSE 流（取其中的 JSON 消息）。"""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    msg = json.loads(payload)
                    if "id" in msg:  # 跳过通知/进度等无 id 消息
                        return msg
        raise McpError("SSE 流中未找到带 id 的 JSON-RPC 响应")
    return resp.json()


async def _rpc(
    url: str,
    method: str,
    params: dict | None = None,
    *,
    session_id: str | None = None,
    token: str | None = None,
    notification: bool = False,
) -> tuple[dict | None, str | None]:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notification:
        body["id"] = 1
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=body, headers=_headers(session_id, token))
        new_session = resp.headers.get("mcp-session-id") or session_id
        if resp.status_code >= 400:
            raise McpError(f"MCP 请求失败 [{resp.status_code}]: {resp.text[:300]}")
        if notification:
            return None, new_session
        msg = _parse_response(resp)
        if "error" in msg:
            raise McpError(f"MCP 错误 ({method}): {msg['error']}")
        return msg.get("result"), new_session


async def connect(url: str, token: str | None = None) -> str:
    """initialize 握手，返回 session id。"""
    _, session_id = await _rpc(
        url,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentforge-console", "version": "0.1.0"},
        },
        token=token,
    )
    if session_id:
        await _rpc(
            url, "notifications/initialized", session_id=session_id, token=token,
            notification=True,
        )
    return session_id or ""


async def list_tools(url: str, token: str | None = None) -> list[dict]:
    """拉取工具清单，返回 [{name, description, input_schema}]。"""
    session_id = await connect(url, token)
    result, _ = await _rpc(
        url, "tools/list", session_id=session_id, token=token,
    )
    tools = []
    for t in (result or {}).get("tools", []):
        tools.append(
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
            }
        )
    return tools


async def call_tool(
    url: str, name: str, arguments: dict, token: str | None = None,
) -> str:
    """调用工具，返回拼接后的文本结果。"""
    session_id = await connect(url, token)
    result, _ = await _rpc(
        url,
        "tools/call",
        {"name": name, "arguments": arguments},
        session_id=session_id,
        token=token,
    )
    if result is None:
        return ""
    if result.get("isError"):
        raise McpError(f"MCP 工具执行出错: {result}")
    parts = [
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)
