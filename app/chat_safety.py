"""会话运行安全补丁（2026-09-07 暂停/继续实战暴露的官方缺陷）。

用户跑「机械控制实验室」团队测试时，暂停团队触发三条官方链路的
竞态/设计冲突，本模块在进程内 patch 修复：

**缺陷 1：迟到确认炸掉整个 reply**
成员/主理人的工具调用停在 ASKING（等待用户确认）→ 用户点暂停
（interrupt）→ 官方清理把等待状态清掉 → 前端确认卡片仍显示 →
用户点「允许」→ ``POST /chat/`` 带 UserConfirmResultEvent →
``Agent._check_incoming_event`` 发现没有等待中的 tool call →
``raise ValueError`` → 整个 reply 标记失败（日志大量 "Reply
failed ... not waiting for confirmation"），并触发唤醒调度器的
重试循环。

修复：patch ``ChatService.run``——入口处检查 UserConfirmResultEvent
的目标 id 是否真在等待；不在（迟到/过期确认，典型于暂停、切换
回复之后）→ 记 warning 直接返回，幂等吞掉，不进 agent 执行。

**缺陷 2：同 id 消息重复追加（时间倒挂）**
官方 ``RedisStorage.upsert_message`` 只检查 list 尾部：尾部 id
相同 → 更新；否则 rpush。中断时刻的时序：
  ① reply 76a8f2d8 流式中，尾部是自己，正常 lset 更新
  ② 暂停 → 中断标记消息（"?"）rpush，尾部变成中断标记
  ③ reply 76a8f2d8 的清理/恢复路径再 upsert → 尾部不是自己 →
     rpush → **同 id 消息在列表里出现两条**（created_at 时间
     倒挂，界面整段重复）
实测主理人会话 20 条消息里两段完整重复。

修复：patch ``upsert_message``——尾部未命中时扫描整个 list 找同
id：找到 → lset 原位更新；找不到 → rpush。会话写入被 session
lock 串行化，扫描-写入窗口无并发竞争；消息量为会话级（几百条）
可接受。

**缺陷 3：团队暂停被自动唤醒穿透（"暂停失效"）**
用户暂停团队后，官方 ``_notify_leader_of_failure``（成员被取消
必须通知 leader）与 inbox 投递都会以 ``input_msg=None`` 唤醒
leader——设计上合理（leader 必须知道成员死了），但与用户的
"团队暂停"语义直接冲突：暂停几秒后 leader 又自动跑起来、又弹
审批卡（用户 2026-09-07 实测两次复现）。

修复：Redis 暂停标志（``agentforge:flow:paused:{user}:{sid}``）。
``/team-flow/pause`` 对 leader+全部成员设标志；本模块 patch
``ChatService.run``：标志存在时拦截 ``input_msg=None`` 的自动
唤醒（日志记录后丢弃；pending inbox 内容保留，resume 后投递）。
``/team-flow/resume`` 清全部标志再 wake。用户直接发消息
（``Msg``）视为手动恢复：清标志放行——用户说话永远不应该被拦。
"""

import logging
from typing import Any

from agentscope.message import Msg

_logger = logging.getLogger("agentforge.chat_safety")

# 团队暂停标志（Redis key 模板）—— 见模块 docstring 缺陷 3
PAUSED_KEY = "agentforge:flow:paused:{user_id}:{session_id}"


async def set_paused(storage: Any, user_id: str, session_ids: list[str]) -> None:
    """为一批会话设置团队暂停标志（穿透拦截）。"""
    client = storage._client  # noqa: SLF001
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    for sid in session_ids:
        await client.set(PAUSED_KEY.format(user_id=user_id, session_id=sid), now)


async def clear_paused(storage: Any, user_id: str, session_ids: list[str]) -> None:
    """清除一批会话的暂停标志（恢复自动唤醒）。"""
    client = storage._client  # noqa: SLF001
    for sid in session_ids:
        await client.delete(PAUSED_KEY.format(user_id=user_id, session_id=sid))


async def is_paused(storage: Any, user_id: str, session_id: str) -> bool:
    client = storage._client  # noqa: SLF001
    return bool(
        await client.get(PAUSED_KEY.format(user_id=user_id, session_id=session_id)),
    )


def patch_chat_safety() -> None:
    """挂载补丁集（幂等，重复调用无副作用）。"""
    _patch_run_safety()
    _patch_upsert_message()


# ---------------------------------------------------------------------------
# 缺陷 1 + 缺陷 3：run 入口拦截（迟到确认 + 暂停穿透）
# ---------------------------------------------------------------------------

_RUN_SAFETY_PATCHED = False


def _patch_run_safety() -> None:
    """ChatService.run：迟到确认静默丢弃 + 暂停期间拦截自动唤醒。"""
    global _RUN_SAFETY_PATCHED
    if _RUN_SAFETY_PATCHED:
        return

    from agentscope.app._service._chat import ChatService
    from agentscope.event import UserConfirmResultEvent
    from agentscope.app.storage import StorageBase

    original_run = ChatService.run

    async def safe_run(
        self: ChatService,
        user_id: str,
        session_id: str,
        agent_id: str,
        input_msg: Any = None,
    ) -> None:
        # ---- 缺陷 3：暂停穿透拦截 ----
        # input_msg=None 是自动唤醒（成员失败通知 / inbox 投递 /
        # resume wake）。团队暂停期间全部拦截，pending inbox 保留，
        # resume 后由下一次唤醒投递。用户主动消息（Msg）放行并
        # 清标志——用户说话永远不被拦。
        if input_msg is None and await is_paused(
            self._storage, user_id, session_id,  # noqa: SLF001
        ):
            _logger.info(
                "会话 %s 处于团队暂停状态，拦截自动唤醒（用户点"
                "「继续」或直接发消息可恢复）",
                session_id,
            )
            return
        from agentscope.message import Msg as _Msg

        if isinstance(input_msg, _Msg) or (
            isinstance(input_msg, list) and input_msg
        ):
            await clear_paused(self._storage, user_id, [session_id])  # noqa: SLF001

        # ---- 缺陷 1：迟到确认拦截 ----
        if isinstance(input_msg, UserConfirmResultEvent):
            storage: StorageBase = self._storage  # noqa: SLF001
            record = await storage.get_session(user_id, agent_id, session_id)
            if record is None:
                # 官方路径自己会报 404 语义，照常进入
                await original_run(
                    self, user_id, session_id, agent_id, input_msg,
                )
                return
            # 尾部 assistant 消息中仍在等待的 tool call id
            # （官方 get_awaiting_tool_calls 按 agent name 过滤，
            #  run 入口拿不到可靠 name，这里直接扫尾部消息——
            #  会话内 context 尾部的 assistant 消息即本 agent 回复）
            awaiting: set[str] = set()
            ctx = record.state.context
            if ctx and ctx[-1].role == "assistant":
                result_ids = {
                    b.id
                    for b in ctx[-1].get_content_blocks("tool_result")
                }
                for tc in ctx[-1].get_content_blocks("tool_call"):
                    if (
                        tc.state == "asking"
                        or (
                            tc.state == "submitted"
                            and tc.id not in result_ids
                        )
                    ) and tc.id:
                        awaiting.add(tc.id)
            event_ids = {
                _.tool_call.id for _ in input_msg.confirm_results
            }
            if event_ids and not (event_ids & awaiting):
                _logger.warning(
                    "丢弃迟到的确认事件：会话 %s 的 tool call %s 已不在"
                    "等待确认（通常因暂停/中断清除了等待状态），"
                    "reply_id=%s",
                    session_id,
                    sorted(event_ids),
                    input_msg.reply_id,
                )
                return
            # 有效确认 = 用户主动处理审批卡 = 手动恢复，解除暂停
            if event_ids:
                await clear_paused(
                    self._storage, user_id, [session_id],  # noqa: SLF001
                )
        await original_run(self, user_id, session_id, agent_id, input_msg)

    ChatService.run = safe_run  # type: ignore[method-assign]
    _RUN_SAFETY_PATCHED = True
    _logger.info("run 入口安全补丁已挂载（迟到确认 + 暂停穿透拦截）")


# ---------------------------------------------------------------------------
# 缺陷 2：upsert_message 全列表幂等
# ---------------------------------------------------------------------------

_UPSERT_PATCHED = False


def _patch_upsert_message() -> None:
    """RedisStorage.upsert_message：同 id 消息全列表去重更新。"""
    global _UPSERT_PATCHED
    if _UPSERT_PATCHED:
        return

    from agentscope.app.storage import RedisStorage

    original_upsert = RedisStorage.upsert_message

    async def idempotent_upsert(
        self: RedisStorage,
        user_id: str,
        session_id: str,
        msg: Msg,
    ) -> None:
        import json as _json

        key = self._message_key(user_id, session_id)  # noqa: SLF001
        client = self._client  # noqa: SLF001
        payload = msg.model_dump_json()
        last_raw = await client.lindex(key, -1)
        if last_raw:
            last_id = _json.loads(last_raw).get("id")
            if last_id == msg.id:
                # 快路径：流式更新，尾部就是自己
                await client.lset(key, -1, payload)
                await self._refresh_key_ttl(key)  # noqa: SLF001
                return
            # 尾部不是同 id：扫描全列表找同 id（中断/恢复路径的
            # 重复追加就是从这里漏出去的）
            items = await client.lrange(key, 0, -1)
            for idx, item in enumerate(items):
                try:
                    if _json.loads(item).get("id") == msg.id:
                        await client.lset(key, idx, payload)
                        await self._refresh_key_ttl(key)  # noqa: SLF001
                        return
                except (ValueError, TypeError):
                    continue
        await client.rpush(key, payload)
        await self._refresh_key_ttl(key)  # noqa: SLF001

    RedisStorage.upsert_message = idempotent_upsert  # type: ignore[method-assign]
    _UPSERT_PATCHED = True
    _logger.info("upsert_message 全列表幂等补丁已挂载")
