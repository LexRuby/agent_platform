"""官方 agent-service 响应契约（单一事实源，供全部测试 mock 引用）。

背景事故（2026-09-03）：三个模块（agent_type / leader_team / team_archive）
都解析官方 ``POST /agent/`` 响应取新建 agent 的 id，但各自只找 ``id`` /
``agent.id`` 字段——真实响应顶层是 ``agent_id``。导致：
- 创建 leader 时类型落回 member；
- 创建 leader 时预置成员名单（team_members）从未存储；
- 封档时新成员注册拿不到 id。

而单元测试的 mock 返回 ``{"id": ...}``，与真实结构不符，测试全绿线上失败。

本模块把从真实服务录制的响应结构固化为此处唯一契约：
所有模拟官方 API 的测试 fixture **必须**通过这里的工厂函数构造响应，
禁止在测试里手写 ``{"id": ...}``` 之类的内联结构——手写结构与契约漂移
时，下方的快照测试会强制同步。

契约录制来源：agentforge 服务 :30000（AgentScope 2.0.7.post1），2026-09-03。
官方版本升级后，应按 DEPLOY.md 用真实服务重新录制并更新此处。
"""

# ── 录制的真实契约快照（顶层键） ────────────────────────────────────────
# POST /agent/ 201 → {"agent_id": "<hex32>"}（无其他顶层键）
POST_AGENT_RESPONSE_KEYS = ["agent_id"]

# GET /agent/ 200 → {"agents": [...], "total": int}；
# 每个 agent：顶层 id/user_id/updated_at/created_at/source/data/editable
LIST_AGENT_RESPONSE_KEYS = ["agents", "total"]
LIST_AGENT_ITEM_KEYS = [
    "id", "user_id", "updated_at", "created_at", "source", "data", "editable",
]

# GET /sessions/{sid}/messages 200 → {"messages": [...], "is_running", "has_more"}
SESSION_MESSAGES_RESPONSE_KEYS = ["messages", "is_running", "has_more"]

# GET /agent/schema/v2 200 → {"schema": {...}}（properties 在内层；
# agent_type / system_prompt.prompt_templates 由叠加中间件注入）
SCHEMA_V2_RESPONSE_KEYS = ["schema"]


def post_agent_response(agent_id: str) -> dict:
    """POST /agent/ 的真实响应结构（顶层 agent_id）。"""
    return {"agent_id": agent_id}


def agent_item(agent_id: str, data: dict, *, user_id: str = "u1") -> dict:
    """GET /agent/ 列表项的真实结构（data 为 AgentData，name 在 data.name）。"""
    return {
        "id": agent_id,
        "user_id": user_id,
        "updated_at": "2026-09-03T00:00:00",
        "created_at": "2026-09-03T00:00:00",
        "source": "user",
        "data": data,
        "editable": True,
    }


def list_agent_response(agents: list[dict]) -> dict:
    """GET /agent/ 的真实响应结构。"""
    return {"agents": agents, "total": len(agents)}


def session_messages_response(messages: list[dict]) -> dict:
    """GET /sessions/{sid}/messages 的真实响应结构。"""
    return {"messages": messages, "is_running": False, "has_more": False}


def schema_v2_response(properties: dict) -> dict:
    """GET /agent/schema/v2 的真实响应结构（schema 包在顶层 "schema" 键下）。"""
    return {"schema": {"title": "AgentData", "type": "object", "properties": properties}}


def message_item(
    msg_id: str, role: str, name: str, content: list[dict],
) -> dict:
    """会话消息项的真实结构（content 为块数组）。"""
    return {
        "id": msg_id,
        "role": role,
        "name": name,
        "content": content,
        "metadata": {},
        "created_at": "2026-09-03T00:00:00",
        "usage": None,
        "finished_at": None,
        "finished_reason": None,
        "structured_output": None,
        "error": None,
    }
