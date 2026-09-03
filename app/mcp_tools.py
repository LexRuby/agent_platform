"""MCP 工具清单查看 API：点开「我的 MCP」看里面有什么、怎么用。

背景（2026-09-03 用户反馈）：官方 Web UI 的 MCP 页只列 name/描述，
注册的 MCP 无法点击查看内部工具，用户无从得知工具能力与参数。
官方 ``/mcp`` router 无工具清单端点，本模块补齐：

- ``GET /mcp-tools/{mcp_id}`` —— 从用户 MCP 库取该 MCP 配置，向其
  server 发起真实连接（HTTP 走 stateless probe，不动用户的 stateful
  会话；stdio 走临时 stateful 连接后即关），返回工具的
  name / description / inputSchema。

鉴权：AuthMiddleware 校验 cookie 后注入 ``X-User-ID``（伪造无效），
身份与官方 router 一致以 user_id 隔离 MCP 库。
"""

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from agentscope.mcp import MCPClient

_logger = logging.getLogger("agentforge.mcp_tools")

mcp_tools_router = APIRouter(tags=["mcp-tools"])

# 官方 app 的 storage（agent_service_app.py init_mcp_tools 注入）
_storage = None


def init_mcp_tools(storage) -> None:
    """注入官方 app 的 storage 单例（与 /mcp router 同一份数据）。"""
    global _storage
    _storage = storage


class MCPToolView(BaseModel):
    """MCP server 内单个工具的定义（name/description 原样透传）。"""

    name: str
    description: str = ""
    input_schema: dict | None = None


class MCPToolsResponse(BaseModel):
    """点开一个 MCP 看到的全部信息。"""

    server: str
    display_name: str | None = None
    description: str = ""
    tools: list[MCPToolView]


async def _list_tools_via_probe(client: MCPClient) -> list[MCPToolView]:
    """向 MCP server 真实连接列出工具。

    HTTP：构造 stateless 副本（不碰用户 record 上可能正挂着的
    stateful 会话）；stdio：临时 stateful 连接，列完即关。
    """
    if client.mcp_config.type == "http_mcp":
        probe = MCPClient(
            name=client.name,
            is_stateful=False,
            mcp_config=client.mcp_config,
        )
        raw_tools = await probe.list_raw_tools()
    else:
        probe = MCPClient(
            name=client.name,
            is_stateful=True,
            mcp_config=client.mcp_config,
        )
        try:
            await probe.connect()
            raw_tools = await probe.list_raw_tools()
        finally:
            await probe.close()
    return [
        MCPToolView(
            name=t.name,
            description=t.description or "",
            input_schema=t.inputSchema,
        )
        for t in raw_tools
    ]


@mcp_tools_router.get(
    "/mcp-tools/{mcp_id}",
    response_model=MCPToolsResponse,
    summary="查看已注册 MCP 的内部工具清单（名称/描述/参数）",
)
async def get_mcp_tools(
    mcp_id: str,
    x_user_id: str = Header(..., alias="X-User-ID"),
) -> MCPToolsResponse:
    if _storage is None:
        raise HTTPException(status_code=503, detail="storage 未初始化")
    record = await _storage.get_mcp(x_user_id, mcp_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"未找到该 MCP（id={mcp_id}）",
        )
    try:
        tools = await _list_tools_via_probe(record.client)
    except Exception as e:  # noqa: BLE001 - 连不上/超时给用户可读的错误
        _logger.warning("MCP %s 工具清单拉取失败: %s", mcp_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"无法连接该 MCP server 获取工具清单：{e}",
        ) from e
    return MCPToolsResponse(
        server=record.client.name,
        display_name=record.display_name,
        description=record.description,
        tools=tools,
    )
