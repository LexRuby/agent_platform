"""零代码控制台 API：MCP 工具注册 / Agent 编排 / 工作空间会话 / 发布。

路由前缀 /api，供 static/index.html 单页控制台调用。
工作空间的 Agent 会话保存在进程内存（单 worker），展示历史持久化到注册中心。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import mcp_client
from app.agent_factory import BUILTIN_TOOLS, build_custom_agent
from app.registry import AGT_DRAFT, AGT_LOCKED, AGT_PUBLISHED, Registry
from app.settings import Settings

router = APIRouter(prefix="/api")

# 由 main.py 注入
settings: Settings | None = None
registry: Registry | None = None

# 工作空间活跃会话（agent_id -> Agent 实例，自带对话记忆）
_sessions: dict = {}


def _init(st: Settings, reg: Registry) -> None:
    global settings, registry
    settings = st
    registry = reg


# ---------- 工具与技能 ----------

@router.get("/tools")
def list_tools():
    """全部可用工具：内置 + 已注册 MCP Server 的工具（含缓存清单）。"""
    result = [
        {
            "ref": f"builtin:{t.name}",
            "name": t.name,
            "description": t.description,
            "source": "builtin",
        }
        for t in BUILTIN_TOOLS
    ]
    for srv in registry.list_mcp_servers():
        for t in srv.get("tools", []):
            result.append(
                {
                    "ref": f"mcp:{srv['name']}:{t['name']}",
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "source": f"mcp:{srv['name']}",
                }
            )
    return result


class McpServerRequest(BaseModel):
    name: str
    url: str
    token: str = ""
    description: str = ""


@router.get("/mcp/servers")
def list_mcp_servers():
    return registry.list_mcp_servers()


@router.post("/mcp/servers")
async def register_mcp_server(req: McpServerRequest):
    """注册 MCP Server 并立即拉取工具清单。"""
    registry.add_mcp_server(req.name, req.url, req.token, req.description)
    try:
        tools = await mcp_client.list_tools(req.url, req.token or None)
    except mcp_client.McpError as exc:
        registry.remove_mcp_server(req.name)
        raise HTTPException(status_code=502, detail=f"MCP 连接失败: {exc}")
    registry.update_mcp_tools(req.name, tools)
    return {"name": req.name, "tools_count": len(tools), "tools": tools}


@router.post("/mcp/servers/{name}/refresh")
async def refresh_mcp_server(name: str):
    """重新拉取工具清单（远端工具变更后用）。"""
    srv = registry.get_mcp_server(name)
    if srv is None:
        raise HTTPException(status_code=404, detail=f"MCP Server 不存在: {name}")
    try:
        tools = await mcp_client.list_tools(srv["url"], srv.get("token") or None)
    except mcp_client.McpError as exc:
        raise HTTPException(status_code=502, detail=f"MCP 连接失败: {exc}")
    registry.update_mcp_tools(name, tools)
    return {"name": name, "tools_count": len(tools), "tools": tools}


@router.delete("/mcp/servers/{name}")
def delete_mcp_server(name: str):
    registry.remove_mcp_server(name)
    return {"ok": True}


# ---------- Agent 编排 ----------

class AgentRequest(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    tools: list[str] = []


@router.get("/agents")
def list_agents(status: str | None = None):
    return registry.list_agents(status)


@router.post("/agents")
def create_agent(req: AgentRequest):
    return registry.create_agent(
        req.name, req.description, req.system_prompt, req.tools,
    )


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    return agent


@router.put("/agents/{agent_id}")
def update_agent(agent_id: str, req: AgentRequest):
    try:
        agent = registry.update_agent(agent_id, req.model_dump())
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    _sessions.pop(agent_id, None)  # 配置变更后重建会话
    return agent


@router.post("/agents/{agent_id}/lock")
def lock_agent(agent_id: str):
    """定稿锁定：draft -> locked，不可再编辑/继续打磨。"""
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    if agent["status"] != AGT_DRAFT:
        raise HTTPException(status_code=409, detail=f"当前状态 {agent['status']} 不可锁定")
    return registry.set_agent_status(agent_id, AGT_LOCKED)


@router.post("/agents/{agent_id}/unlock")
def unlock_agent(agent_id: str):
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    if agent["status"] != AGT_LOCKED:
        raise HTTPException(status_code=409, detail=f"当前状态 {agent['status']} 不可解锁")
    return registry.set_agent_status(agent_id, AGT_DRAFT)


@router.post("/agents/{agent_id}/publish")
def publish_agent(agent_id: str):
    """发布：locked -> published，对外可见可对话。"""
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    if agent["status"] != AGT_LOCKED:
        raise HTTPException(status_code=409, detail="仅 locked 状态可发布（请先锁定定稿）")
    return registry.set_agent_status(agent_id, AGT_PUBLISHED)


# ---------- 工作空间（打磨团队 Agent） ----------

@router.get("/agents/{agent_id}/workspace")
def get_workspace(agent_id: str):
    if registry.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    return registry.get_workspace(agent_id)


@router.delete("/agents/{agent_id}/workspace")
def clear_workspace(agent_id: str):
    registry.clear_workspace(agent_id)
    _sessions.pop(agent_id, None)
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str


async def _chat(agent_id: str, message: str, refine_mode: bool) -> str:
    agent_def = registry.get_agent(agent_id)
    if agent_def is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    if refine_mode and agent_def["status"] != AGT_DRAFT:
        raise HTTPException(
            status_code=409, detail="Agent 已锁定/发布，工作空间仅对 draft 开放",
        )

    from agentscope.message import UserMsg

    if settings.fake_llm:
        reply = (
            f"[demo] 收到：{message[:100]}（fake-llm 模式，未调用大模型与工具）。"
            f"当前 Agent 装配工具 {len(agent_def.get('tools', []))} 个。"
        )
    else:
        agent = _sessions.get(agent_id)
        if agent is None:
            agent = build_custom_agent(settings, registry, agent_def)
            _sessions[agent_id] = agent
        result = await agent.reply(UserMsg(name="user", content=message))
        reply = result.get_text_content()
    return reply


@router.post("/agents/{agent_id}/chat")
async def workspace_chat(agent_id: str, req: ChatRequest):
    """工作空间对话：持续打磨 draft Agent 的运作方式，历史持久化。"""
    registry.append_message(agent_id, "user", req.message)
    reply = await _chat(agent_id, req.message, refine_mode=True)
    registry.append_message(agent_id, "assistant", reply)
    return {"reply": reply}


# ---------- 发布大厅（对外用户使用） ----------

@router.get("/published")
def list_published():
    return registry.list_agents(AGT_PUBLISHED)


@router.post("/published/{agent_id}/chat")
async def published_chat(agent_id: str, req: ChatRequest):
    """对外用户对话入口：仅对已发布 Agent 开放，会话独立于工作空间。"""
    agent_def = registry.get_agent(agent_id)
    if agent_def is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {agent_id}")
    if agent_def["status"] != AGT_PUBLISHED:
        raise HTTPException(status_code=404, detail="Agent 未发布")
    reply = await _chat(agent_id, req.message, refine_mode=False)
    return {"reply": reply}
