"""团队分支：工作流节点级 fork（2026-09-08 用户需求：分支对比培育团队）。

用户语义（2026-09-08 确认）：
在工作流图上点击任意专家节点 → 查看该节点产出 → 选择重跑方式：
- **新建分支**：当前会话原样保留（旧结果不动），fork 出新会话——
  消息截断到该节点汇报所在消息、context 同步截断、共享文件工作区
  与团队——然后在新分支重新处理后续整条链路。多分支并存、可对比，
  选择最合理的结果，是培育 team 的"实验田"。
- **覆盖重跑**：等价既有 truncate（session_flow，前端直接调用），
  本模块不重复实现。

fork 的关键技术决策：
- **storage.upsert_session(session_id=None) 必新建**——绕过官方
  create_session 端点的 (user, agent, workspace) 三元组去重。
- **共享 workspace_id**（config 原样复制）：分支重跑 = 在既有代码
  成果上改进，而非从空目录重来（培育语义）。
- **团队接管**：新分支 set_session_team_id(原 team) 且
  team.session_id 改指新分支。官方 TeamSay 的 directory 校验
  （own_session_ids）决定一个 team 同一时刻只有一个 leader 会话
  能调度——fork 后调度权移交新分支；旧分支成为可回看对比的历史
  存档（前端 team 视图按 session.team_id 查询，显示不受影响）。
- **成员会话复用**：不 fork 成员会话，成员带着完整历史接收新分派
  （"基于上次成果改进"的培育直觉）；成员汇报经 directory 路由到
  新分支（team.session_id 已改），旧分支天然隔离不受打扰。
- **state 截断语义与 session_flow 一致**：summary 清空、
  reply_context 重置、middle_context 清空；permission / tasks
  context 保留（已授权工具不必重新授权）。

端点（team_fork_router，tag: agentforge）：
- ``POST /sessions/{sid}/team-fork``   节点级 fork 新分支
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agentscope.state import AgentState, ReplyContext

from app.session_flow import (
    _ensure_not_running,
    _find_message_index,
    _parse_msg,
    _require_user,
    _truncate_context_at,
)

_logger = logging.getLogger("agentforge.team_fork")

team_fork_router = APIRouter(tags=["agentforge"])


class TeamForkRequest(BaseModel):
    """节点级 fork 请求。"""

    agent_id: str = Field(description="主理人（leader）agent id")
    message_id: str = Field(
        description="分支锚点：保留该消息及之前的全部历史"
        "（通常是所选节点的成员汇报所在消息）",
    )
    name: str | None = Field(
        default=None,
        description="新分支会话名（默认自动生成「分支 · …」）",
    )


class TeamForkResponse(BaseModel):
    """节点级 fork 响应。"""

    session_id: str = Field(description="新分支会话 id")
    parent_session_id: str = Field(description="原会话 id")
    kept_messages: int = Field(description="复制到新分支的消息数")
    team_taken_over: bool = Field(description="团队调度权是否移交新分支")


def _branch_name(orig_name: str, node_label: str) -> str:
    """生成分支会话名：分支 · 原名 · 自节点。"""
    base = (orig_name or "团队会话").strip()[:30]
    node = (node_label or "").strip()[:20]
    return f"分支 · {base} · 自{node}" if node else f"分支 · {base}"


@team_fork_router.post(
    "/sessions/{session_id}/team-fork",
    response_model=TeamForkResponse,
    status_code=201,
    summary="工作流节点级 fork：保留旧结果，新分支重跑后续链路",
)
async def fork_team_session(
    session_id: str,
    body: TeamForkRequest,
    request: Request,
) -> TeamForkResponse:
    """从工作流某节点 fork 新分支。

    保留 ``message_id`` 及之前的全部消息复制到新会话（原会话不动），
    context 同步截断到同一位置；新会话共享原 workspace 与团队。
    团队调度权（TeamSay 的 leader directory）移交新分支。

    仅限 owner 本人；running 会话返回 409（先暂停再 fork）。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    bus = request.app.state.message_bus

    record = await storage.get_session(
        user_id, body.agent_id, session_id,
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"会话 {session_id} 不存在（或不在当前账号下）",
        )
    await _ensure_not_running(bus, session_id)

    client = storage._client  # noqa: SLF001 — 官方无批量复制接口
    src_key = (
        storage.key_config.messages.format(
            user_id=user_id, session_id=session_id,
        )
        if hasattr(storage, "key_config")
        else f"agentscope:user:{user_id}:session:{session_id}:messages"
    )

    found = await _find_message_index(client, src_key, body.message_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"消息 {body.message_id} 不存在于该会话",
        )
    idx, anchor_msg = found

    # 1. 新建分支会话：config 原样复制（共享 workspace），state 稍后写
    config = record.config.model_copy(deep=True)
    fork_name = body.name or _branch_name(
        config.name or "", body.name or "",
    )
    config.name = fork_name
    new_record = await storage.upsert_session(
        user_id=user_id,
        agent_id=body.agent_id,
        config=config,
        state=AgentState(),
    )
    fork_sid = new_record.id
    dst_key = src_key.replace(
        f"session:{session_id}:messages", f"session:{fork_sid}:messages",
    )

    # 2. 复制消息前缀（原 raw 直传，避免反/再序列化损耗）
    prefix_raws = await client.lrange(src_key, 0, idx)
    if prefix_raws:
        await client.rpush(dst_key, *prefix_raws)

    # 3. context 同步截断（LLM 只认 state.context）
    state: AgentState = record.state.model_copy(deep=True)
    state.context = _truncate_context_at(
        state.context,
        body.message_id,
        str(getattr(anchor_msg, "created_at", "") or ""),
    )
    state.summary = ""
    state.reply_context = ReplyContext()
    state.middle_context = {}
    await storage.update_session_state(
        user_id=user_id, agent_id=body.agent_id,
        session_id=fork_sid, state=state,
    )

    # 4. 团队接管：分支绑定原团队，活跃 leader 指针移交
    taken_over = False
    team_id: Any = getattr(record, "team_id", None)
    if team_id:
        team = await storage.get_team(user_id, team_id)
        if team is not None:
            await storage.set_session_team_id(
                user_id, fork_sid, team_id,
            )
            team.session_id = fork_sid
            await storage.upsert_team(user_id, team)
            taken_over = True
        else:  # 团队记录已缺失（软解散残留）——分支退化为普通会话
            _logger.warning(
                "fork 时团队记录缺失，分支不接管: user=%s team=%s",
                user_id, team_id,
            )

    _logger.info(
        "工作流节点 fork: user=%s parent=%s fork=%s 锚点=%s 保留=%d 接管=%s",
        user_id, session_id, fork_sid, body.message_id,
        idx + 1, taken_over,
    )
    return TeamForkResponse(
        session_id=fork_sid,
        parent_session_id=session_id,
        kept_messages=idx + 1,
        team_taken_over=taken_over,
    )
