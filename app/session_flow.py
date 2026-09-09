"""会话流程控制：暂停/继续、任意位置重新对话、流程重启（2026-09-08 v3）。

用户需求（2026-09-08 方案确认）：
1. **暂停/继续** —— "停止 + 可继续"语义：暂停 = 中断本次回复但上下文
   完整保留（官方 interrupt 三态幂等：running 取消任务 / HITL parked
   唤醒中断 / idle no-op）；继续 = 触发官方"从当前状态继续"
   （``input: None`` 的 wake 语义）。团队级暂停同时取消全部成员运行。
2. **任意位置重新对话** —— 选中历史某条消息，从该点分叉：之后的
   消息**归档后删除**（副本可查），上下文同步截断，会话身份/团队
   绑定/历史前缀全保留。
3. **流程重新启动** —— 模型上下文归零重新开始，消息历史保留可回看；
   团队 leader 重启时先停止全部成员运行，团队结构不动。

关键技术事实（决定实现方式）：
- LLM 上下文的唯一来源是 ``AgentState.context``（reply 时增量维护、
  结束后 ``update_session_state`` 持久化），消息 Redis List 只是展示
  层记录——**截断必须两者同步**，只删消息列表 LLM 仍"记得"被删内容。
- 官方 storage 只有 upsert/get/list 消息接口，无截断；本模块直接
  ``storage._client`` 对 Redis List 做 LTRIM（fakeredis 同样支持，
  测试无需真实 Redis）。
- 团队成员与 leader 通过 ``TeamRecord.data.members[].session_id``
  关联（team_preserve 同源），停止成员用官方
  ``SessionService.cancel_session_run``（幂等，广播取消 + 等锁清）。

端点（session_flow_router，tag: agentforge）：
- ``POST /sessions/{sid}/truncate``     任意位置重新对话（归档+截断）
- ``POST /sessions/{sid}/restart``      流程重启（清上下文保留历史）
- ``POST /team-flow/{leader_sid}/pause``   团队暂停（leader+成员全停）
- ``POST /team-flow/{leader_sid}/resume``  团队继续（wake leader）
- ``GET  /sessions/{sid}/flow-archive``    截断归档查询

归档结构（Redis List，新截断 RPUSH 到尾部）：
``agentforge:flow-archive:{user}:{sid}`` → JSON
``{truncated_at, from_message_id, removed_count, messages: [...]}``
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agentscope.app._bus_ops import enqueue_run_trigger
from agentscope.app.message_bus import MessageBusKeys
from agentscope.state import ReplyContext

from app.chat_safety import clear_paused, set_paused

_logger = logging.getLogger("agentforge.session_flow")

session_flow_router = APIRouter(tags=["agentforge"])

# Redis 键模板
_ARCHIVE_KEY = "agentforge:flow-archive:{user_id}:{session_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_user(request: Request) -> str:
    """认证中间件注入的 X-User-ID（与 agent_share 同模式）。"""
    user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="未认证")
    return user_id


# ---------------------------------------------------------------- 请求模型


class TruncateRequest(BaseModel):
    """任意位置重新对话：保留 message_id 及之前的消息。"""

    agent_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)


class RestartRequest(BaseModel):
    """流程重启：清上下文，保留消息历史。"""

    agent_id: str = Field(min_length=1)


class FlowOpResponse(BaseModel):
    """流程操作结果（统一形状）。"""

    session_id: str
    kept_messages: int = 0
    archived_messages: int = 0
    cancelled_members: int = 0


# ---------------------------------------------------------------- 内部工具


async def _get_session_record(storage: Any, user_id: str, agent_id: str,
                               session_id: str) -> Any:
    record = await storage.get_session(user_id, agent_id, session_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"会话 {session_id} 不存在（或不在当前账号下）",
        )
    return record


async def _ensure_not_running(bus: Any, session_id: str) -> None:
    """running（锁被 worker 持有）时禁止截断/重启——竞态会撕裂状态。"""
    if await bus.is_locked(MessageBusKeys.session_lock(session_id)):
        raise HTTPException(
            status_code=409,
            detail="会话正在运行中，请先暂停（中断）后再执行此操作",
        )


def _parse_msg(raw: str) -> Any:
    """Redis List 条目 → Msg（官方存储序列化为 Msg.model_dump_json）。"""
    from agentscope.message import Msg

    return Msg.model_validate_json(raw)


async def _find_message_index(client: Any, key: str,
                              message_id: str) -> tuple[int, Any] | None:
    """在消息 List 里定位 message_id → (索引, Msg)。

    官方存储倒序扫描；消息量级（百级）下全量 lrange 一次更简单，
    避免逐条 lindex 的往返。
    """
    raws = await client.lrange(key, 0, -1)
    for i, raw in enumerate(raws):
        msg = _parse_msg(raw)
        if msg.id == message_id:
            return i, msg
    return None


def _truncate_context_at(context: list, message_id: str,
                          anchor_created_at: str | None) -> list:
    """同步截断 AgentState.context。

    优先按消息 id 精确定位；该消息不在 context（如团队汇报等展示层
    消息）时退化为时间锚点：保留 created_at <= 锚点时刻的尾部之前
    的消息。两者都找不到（context 远落后于消息列表）则保留原 context
    ——宁少删不误删。
    """
    for i, m in enumerate(context):
        if m.id == message_id:
            return list(context[: i + 1])
    if anchor_created_at:
        for i in range(len(context) - 1, -1, -1):
            m = context[i]
            m_at = getattr(m, "created_at", None)
            if m_at is not None and str(m_at) <= anchor_created_at:
                return list(context[: i + 1])
    return list(context)


async def _reset_session_state(storage: Any, user_id: str, agent_id: str,
                               session_id: str, *,
                               new_context: list | None) -> int:
    """重置会话运行状态并写回存储，返回保留的消息数。

    - ``new_context=None``：流程重启语义（context 清零）
    - ``new_context=[...]``：截断语义（context 截到指定前缀）

    统一处理：summary 清空（旧摘要引用被删内容会污染新对话）、
    reply_context 重置（下一条消息开新回复）、middle_context 清空
    （中间件跨回复状态随旧流程作废）；permission_context /
    tasks_context 保留（用户已授权的工具不必重新授权、任务清单
    与对话截断正交）。
    """
    record = await storage.get_session(user_id, agent_id, session_id)
    state = record.state
    state.context = new_context if new_context is not None else []
    state.summary = ""
    state.reply_context = ReplyContext()
    state.middle_context = {}
    await storage.update_session_state(
        user_id=user_id, agent_id=agent_id,
        session_id=session_id, state=state,
    )
    return len(state.context)


async def _leader_members(storage: Any, user_id: str,
                          leader_session_id: str) -> list[dict]:
    """leader 会话 → 当前有效团队的成员清单（含成员 session_id）。

    与 team_preserve 的 /team-sessions 同源逻辑，但只取未解散团队
    （leader session 的 team_id 仍指向该 team 记录）。
    """
    result: list[dict] = []
    teams = await storage.list_teams(user_id)
    for team in teams:
        if team.session_id != leader_session_id:
            continue
        leader_session = await storage.get_session(
            user_id, team.leader_agent_id, team.session_id,
        )
        # 解散判定：leader 的 team_id 已被软解散清空
        if leader_session is None or leader_session.team_id != team.id:
            continue
        for m in getattr(getattr(team, "data", None), "members", None) or []:
            agent_id = m.get("agent_id") if isinstance(m, dict) else m.agent_id
            m_session = (
                m.get("session_id") if isinstance(m, dict) else m.session_id
            )
            if m_session:
                result.append(
                    {"agent_id": agent_id, "session_id": m_session},
                )
    return result


async def _cancel_members(session_service: Any, members: list[dict]) -> int:
    """取消成员运行（幂等；尽力而为，单个失败不阻断整体）。"""
    cancelled = 0
    for m in members:
        try:
            await session_service.cancel_session_run(m["session_id"])
            cancelled += 1
        except Exception:  # noqa: BLE001 — 成员可能已停，尽力而为
            _logger.exception(
                "取消成员运行失败: member=%s", m.get("agent_id"),
            )
    return cancelled


# ---------------------------------------------------------------- 端点


@session_flow_router.post(
    "/sessions/{session_id}/truncate",
    response_model=FlowOpResponse,
    summary="任意位置重新对话：归档并截断 message_id 之后的消息",
)
async def truncate_session(
    session_id: str,
    body: TruncateRequest,
    request: Request,
) -> FlowOpResponse:
    """从指定消息处重新对话。

    保留 ``message_id`` 及之前的全部消息；之后的消息先写入归档
    （``agentforge:flow-archive:*``，可查可恢复）再从消息 List 删除
    （LTRIM），``AgentState.context`` 同步截断到同一位置。

    仅限 owner 本人（会话按 user_id 键控天然隔离）；running 会话
    返回 409（先暂停再截断，避免与持久化写竞态撕裂状态）。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    bus = request.app.state.message_bus

    await _get_session_record(storage, user_id, body.agent_id, session_id)
    await _ensure_not_running(bus, session_id)

    client = storage._client  # noqa: SLF001 — 官方无截断接口，直接操作 List
    key = (
        storage.key_config.messages.format(
            user_id=user_id, session_id=session_id,
        )
        if hasattr(storage, "key_config")
        else f"agentscope:user:{user_id}:session:{session_id}:messages"
    )

    found = await _find_message_index(client, key, body.message_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"消息 {body.message_id} 不存在于该会话",
        )
    idx, msg = found

    removed_raws = await client.lrange(key, idx + 1, -1)
    removed = [_parse_msg(r) for r in removed_raws]

    # 1. 归档（先写后删：中途失败最多重复归档，不会丢内容）
    if removed:
        archive = {
            "truncated_at": _now_iso(),
            "from_message_id": body.message_id,
            "removed_count": len(removed),
            "messages": [m.model_dump(mode="json") for m in removed],
        }
        await client.rpush(
            _ARCHIVE_KEY.format(user_id=user_id, session_id=session_id),
            json.dumps(archive, ensure_ascii=False),
        )

    # 2. 截断消息 List（保留 0..idx）
    await client.ltrim(key, 0, idx)

    # 3. 同步截断上下文（LLM 只认 state.context，必须一起截）
    record = await storage.get_session(
        user_id, body.agent_id, session_id,
    )
    state = record.state
    state.context = _truncate_context_at(
        state.context,
        body.message_id,
        str(getattr(msg, "created_at", "") or ""),
    )
    state.summary = ""
    state.reply_context = ReplyContext()
    state.middle_context = {}
    await storage.update_session_state(
        user_id=user_id, agent_id=body.agent_id,
        session_id=session_id, state=state,
    )

    _logger.info(
        "任意位置重新对话: user=%s session=%s 截断点=%s 保留=%d 归档=%d",
        user_id, session_id, body.message_id, idx + 1, len(removed),
    )
    return FlowOpResponse(
        session_id=session_id,
        kept_messages=idx + 1,
        archived_messages=len(removed),
    )


@session_flow_router.post(
    "/sessions/{session_id}/restart",
    response_model=FlowOpResponse,
    summary="流程重启：上下文归零，保留消息历史",
)
async def restart_session(
    session_id: str,
    body: RestartRequest,
    request: Request,
) -> FlowOpResponse:
    """重新启动会话流程（"换个思路重做"）。

    ``AgentState.context`` / ``summary`` / ``reply_context`` /
    ``middle_context`` 全部归零（下次消息从零开始推理），消息历史
    保留在会话里可回看；``permission_context`` / ``tasks_context``
    保留（已授权工具不必重新授权）。

    团队 leader 会话：先取消全部成员的运行（团队结构、成员 agent
    与成员会话全部保留），leader 的 team 绑定不动——重启后大A 仍在
    团队中可继续调度。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    bus = request.app.state.message_bus
    session_service = request.app.state.session_service

    record = await _get_session_record(
        storage, user_id, body.agent_id, session_id,
    )
    await _ensure_not_running(bus, session_id)

    # 团队 leader：先停成员（成员 session 键控在 user 下，与 leader 同 owner）
    cancelled = 0
    members = await _leader_members(storage, user_id, session_id)
    if members:
        cancelled = await _cancel_members(session_service, members)

    # leader 自身若 parked 在 HITL（无 run 锁）也一并中断，避免残留确认卡
    if record.team_id:
        try:
            chat_service = request.app.state.chat_service
            await chat_service.interrupt(
                user_id, session_id, body.agent_id,
            )
        except Exception:  # noqa: BLE001 — parked 中断尽力而为
            _logger.exception(
                "重启时中断 HITL 停靠失败: session=%s", session_id,
            )

    kept = await _reset_session_state(
        storage, user_id, body.agent_id, session_id, new_context=None,
    )

    _logger.info(
        "流程重启: user=%s session=%s 历史保留=全部 上下文清零 成员停=%d",
        user_id, session_id, cancelled,
    )
    return FlowOpResponse(
        session_id=session_id,
        kept_messages=kept,
        cancelled_members=cancelled,
    )


@session_flow_router.post(
    "/team-flow/{leader_session_id}/pause",
    response_model=FlowOpResponse,
    summary="团队暂停：中断 leader + 取消全部成员运行",
)
async def pause_team_flow(
    leader_session_id: str,
    request: Request,
    agent_id: str = Query(description="leader 的 agent id"),
) -> FlowOpResponse:
    """暂停整个团队流程。

    leader 走官方 ``ChatService.interrupt``（三态幂等：running 取消
    本次回复 / HITL parked 唤醒中断 / idle 静默）；全部成员走
    ``cancel_session_run``（广播取消）。上下文全部保留——"停止 +
    可继续"语义，``/resume`` 可随时唤醒。

    同时设置团队暂停标志（leader+成员）：暂停期间拦截一切自动
    唤醒（成员失败通知、inbox 投递），防止"暂停几秒后 leader 又
    自动跑起来"（2026-09-07 实测：官方 _notify_leader_of_failure
    会唤醒 leader 继续干活、再弹审批卡）。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    chat_service = request.app.state.chat_service
    session_service = request.app.state.session_service

    record = await _get_session_record(
        storage, user_id, agent_id, leader_session_id,
    )

    # 无在册团队（已解散/未组队）→ 409：不存在"暂停一个不存在的
    # 团队"，且避免给 leader 误设暂停标志阻止后续正常对话
    # （2026-09-09 用户反馈：设计语言一致性）
    if not record.team_id:
        raise HTTPException(
            status_code=409,
            detail="该会话没有在册团队（已解散或尚未组队），无法执行团队暂停",
        )
    members = await _leader_members(storage, user_id, leader_session_id)
    cancelled = await _cancel_members(session_service, members)

    await chat_service.interrupt(user_id, leader_session_id, agent_id)

    # 暂停标志：leader + 全部成员（穿透拦截，见 app.chat_safety）
    member_sids = [m["session_id"] for m in members if m.get("session_id")]
    await set_paused(storage, user_id, [leader_session_id, *member_sids])

    _logger.info(
        "团队暂停: user=%s leader_session=%s members=%d cancelled=%d "
        "team=%s（暂停标志已设置，自动唤醒将被拦截）",
        user_id, leader_session_id, len(members), cancelled, record.team_id,
    )
    return FlowOpResponse(
        session_id=leader_session_id,
        cancelled_members=cancelled,
    )


@session_flow_router.post(
    "/team-flow/{leader_session_id}/resume",
    response_model=FlowOpResponse,
    summary="团队继续：唤醒 leader 从当前状态继续",
)
async def resume_team_flow(
    leader_session_id: str,
    request: Request,
    agent_id: str = Query(description="leader 的 agent id"),
) -> FlowOpResponse:
    """继续团队流程。

    先清除全部暂停标志（leader+成员，恢复自动唤醒），再对 leader
    enqueue 一个 ``wake`` 触发（官方 ``input: None`` 语义：从当前
    状态继续推理——上下文完整保留，上一步停在哪个 ReAct 轮，继续
    时就从那里接着思考）。成员不直接唤醒：大A 被唤醒后自行通过
    TeamSay / 等待成员回报恢复调度（官方异步消息驱动模型）；暂停
    期间积压的 inbox 消息（成员失败通知等）也会在 leader 恢复
    运行时投递。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    bus = request.app.state.message_bus

    await _get_session_record(storage, user_id, agent_id, leader_session_id)

    # 先清标志再 wake——顺序关键：wake 到达 run 入口时标志必须已清除
    members = await _leader_members(storage, user_id, leader_session_id)
    member_sids = [m["session_id"] for m in members if m.get("session_id")]
    await clear_paused(storage, user_id, [leader_session_id, *member_sids])

    await enqueue_run_trigger(
        bus,
        user_id=user_id,
        session_id=leader_session_id,
        agent_id=agent_id,
        kind=MessageBusKeys.WAKEUP_KIND_WAKE,
        inputs=None,
    )

    _logger.info(
        "团队继续: user=%s leader_session=%s", user_id, leader_session_id,
    )
    return FlowOpResponse(session_id=leader_session_id)


@session_flow_router.post(
    "/team-flow/{leader_session_id}/dissolve",
    response_model=FlowOpResponse,
    summary="解散团队（用户主动，软解散语义）",
)
async def dissolve_team_flow(
    leader_session_id: str,
    request: Request,
    agent_id: str = Query(description="leader 的 agent id"),
) -> FlowOpResponse:
    """用户主动解散团队（2026-09-09：LLM 的 TeamDelete 已被无条件
    DENY，解散的唯一入口）。

    走 patched ``SessionService.delete_team``（软解散）：取消全部
    成员正在运行的任务（不删记录）→ 解除 leader 会话的团队绑定。
    成员 agent / 成员会话 / 团队记录**全部保留**（培养资产）。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    session_service = request.app.state.session_service

    record = await _get_session_record(
        storage, user_id, agent_id, leader_session_id,
    )
    if not record.team_id:
        raise HTTPException(
            status_code=409,
            detail="该会话没有在册团队（已解散或尚未组队），无需解散",
        )

    # 成员数用于反馈；软解散后绑定即解除
    members = await _leader_members(storage, user_id, leader_session_id)
    ok = await session_service.delete_team(user_id, record.team_id)
    if not ok:
        raise HTTPException(status_code=500, detail="解散失败，请重试")

    _logger.info(
        "团队解散（用户主动，资产保留）: user=%s leader_session=%s "
        "team=%s members=%d",
        user_id, leader_session_id, record.team_id, len(members),
    )
    return FlowOpResponse(
        session_id=leader_session_id,
        cancelled_members=len(members),
    )


@session_flow_router.get(
    "/sessions/{session_id}/flow-archive",
    summary="查询该会话的截断归档（被删消息副本）",
)
async def get_flow_archive(
    session_id: str,
    request: Request,
    agent_id: str = Query(description="会话归属的 agent id"),
) -> dict:
    """列出历次截断归档（新截断在尾部）。

    每条：``{truncated_at, from_message_id, removed_count, messages}``。
    供前端"查看被重开删除的内容"入口使用。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage

    await _get_session_record(storage, user_id, agent_id, session_id)

    client = storage._client  # noqa: SLF001 — 只读归档 List
    raws = await client.lrange(
        _ARCHIVE_KEY.format(user_id=user_id, session_id=session_id), 0, -1,
    )
    archives = [json.loads(r) for r in raws]
    return {"session_id": session_id, "archives": archives}
