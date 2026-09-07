"""chat_safety 补丁回归测试（2026-09-07 团队实测三缺陷）。

1. 迟到确认：暂停后到达的 UserConfirmResultEvent 被静默丢弃，
   不再炸 reply（ValueError → "Reply failed ... not waiting"）。
2. upsert_message 幂等：中断标记消息插在尾部后，同 id reply
   的再写入必须原位更新，不得 rpush 重复（时间倒挂/整段重复）。
3. 团队暂停穿透：暂停标志存在时 input=None 的自动唤醒被拦截；
   用户 Msg / 有效确认放行并清标志。

判定方式：monkeypatch ``ChatService._run_impl`` 为记录器——
官方 ``run`` 会吞掉一切异常（docstring 明示），不能用"是否抛"
判断拦截与否，必须看执行是否到达 agent 层。
"""

from dataclasses import dataclass, field

import fakeredis
import pytest
from agentscope.app.storage import RedisStorage
from agentscope.event import UserConfirmResultEvent
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.message._block import ToolCallState
from agentscope.state import AgentState

from app.chat_safety import (
    clear_paused,
    is_paused,
    patch_chat_safety,
    set_paused,
)

U = "u1"
AGENT = "a-1"
SID = "s-1"


# ---------------------------------------------------------------- 工具


def _seed_session(fake, storage, state: AgentState) -> None:
    from agentscope.app.storage._model._session import (
        SessionConfig,
        SessionRecord,
    )

    record = SessionRecord(
        user_id=U,
        agent_id=AGENT,
        session_id=SID,
        config=SessionConfig(workspace_id="ws-1"),
        state=state,
    )
    key = storage.key_config.session.format(user_id=U, session_id=SID)
    fake.set(key, record.model_dump_json())


def _confirm_event(call_id: str, reply_id: str = "reply-x") -> UserConfirmResultEvent:
    """构造带单个 tool call 的确认事件（confirmed=True）。"""
    from agentscope.event import ConfirmResult
    from agentscope.permission import PermissionRule

    return UserConfirmResultEvent(
        reply_id=reply_id,
        confirm_results=[
            ConfirmResult(
                confirmed=True,
                tool_call=ToolCallBlock(
                    type="tool_call",
                    id=call_id,
                    name="Bash",
                    input='{"command": "ls"}',
                    state=ToolCallState.ASKING,
                ),
                rules=[
                    PermissionRule(
                        tool_name="Bash",
                        rule_content="ls:*",
                        behavior="allow",
                        source="test",
                    ),
                ],
            ),
        ],
    )


@dataclass
class _ImplRecorder:
    """记录 _run_impl 是否被调用及其参数（run 的直通层）。"""

    calls: list[object] = field(default_factory=list)

    async def __call__(self, user_id, session_id, agent_id, input_msg=None):
        self.calls.append(input_msg)


@pytest.fixture
def env(monkeypatch):
    """fakeredis + 官方 storage + 挂好 patch 的最小 ChatService。

    _run_impl 换成记录器：patch 的 safe_run → original run →
    _run_impl（记录器）。calls 空 = 被拦截；calls 有输入 = 放行。
    """
    patch_chat_safety()

    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    afake = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    storage = RedisStorage()
    storage._client = afake  # noqa: SLF001

    from agentscope.app._service._chat import ChatService

    rec = _ImplRecorder()
    monkeypatch.setattr(ChatService, "_run_impl", rec)

    svc = ChatService.__new__(ChatService)  # 不走 __init__（重依赖）
    svc._storage = storage  # noqa: SLF001

    return {"fake": fake, "storage": storage, "svc": svc, "rec": rec}


# ================================================================ 缺陷 1：迟到确认


class TestLateConfirmation:
    async def test_late_confirm_dropped(self, env):
        """无等待中的 tool call：确认事件被丢弃，不进 agent 层。"""
        _seed_session(env["fake"], env["storage"], AgentState(session_id=SID))

        await type(env["svc"]).run(
            env["svc"], U, SID, AGENT, _confirm_event("call-gone"),
        )
        assert env["rec"].calls == [], "迟到确认必须被拦截"

    async def test_valid_confirm_passes_through(self, env):
        """有等待中的确认：事件放行到 agent 层。"""
        state = AgentState(session_id=SID)
        assistant = Msg(
            id="m-1", role="assistant", name="agent",
            content=[
                ToolCallBlock(
                    type="tool_call", id="call-live", name="Bash",
                    input='{"command": "ls"}',
                    state=ToolCallState.ASKING,
                ),
            ],
        )
        state.context = [assistant]
        _seed_session(env["fake"], env["storage"], state)

        await type(env["svc"]).run(
            env["svc"], U, SID, AGENT, _confirm_event("call-live"),
        )
        assert len(env["rec"].calls) == 1, "有效确认必须放行"


# ================================================================ 缺陷 2：upsert 幂等


class TestUpsertIdempotent:
    async def test_same_id_after_interruption_marker(self, env):
        """尾部是中断标记时，同 id 消息必须原位更新不重复。"""
        from agentscope.app.storage import RedisStorage as RS

        reply = Msg(
            id="reply-1", role="assistant", name="agent",
            content=[TextBlock(type="text", text="流式内容 v1")],
        )
        await RS.upsert_message(env["storage"], U, SID, reply)

        marker = Msg(
            id="marker-1", role="assistant", name="agent",
            content=[TextBlock(type="text", text="?")],
        )
        await RS.upsert_message(env["storage"], U, SID, marker)

        # 中断后 reply 的清理路径再写入（内容更完整）
        reply2 = Msg(
            id="reply-1", role="assistant", name="agent",
            content=[TextBlock(type="text", text="流式内容 v2 完整")],
        )
        await RS.upsert_message(env["storage"], U, SID, reply2)

        key = env["storage"].key_config.messages.format(
            user_id=U, session_id=SID,
        )
        items = await env["storage"]._client.lrange(key, 0, -1)  # noqa: SLF001
        assert len(items) == 2, "同 id 消息不得重复追加"
        import json

        first, second = (json.loads(x) for x in items)
        assert first["id"] == "reply-1"
        assert second["id"] == "marker-1"
        assert "v2 完整" in first["content"][0]["text"], "必须更新为最新内容"

    async def test_new_id_appends(self, env):
        """新 id 消息正常 rpush（基础回归）。"""
        from agentscope.app.storage import RedisStorage as RS

        m1 = Msg(
            id="a", role="user", name="u",
            content=[TextBlock(type="text", text="1")],
        )
        m2 = Msg(
            id="b", role="user", name="u",
            content=[TextBlock(type="text", text="2")],
        )
        await RS.upsert_message(env["storage"], U, SID, m1)
        await RS.upsert_message(env["storage"], U, SID, m2)

        key = env["storage"].key_config.messages.format(
            user_id=U, session_id=SID,
        )
        items = await env["storage"]._client.lrange(key, 0, -1)  # noqa: SLF001
        assert len(items) == 2

    async def test_streaming_tail_update(self, env):
        """尾部同 id：快路径 lset 更新（官方原行为保留）。"""
        from agentscope.app.storage import RedisStorage as RS

        m = Msg(id="a", role="assistant", name="agent",
                content=[TextBlock(type="text", text="v1")])
        await RS.upsert_message(env["storage"], U, SID, m)
        m2 = Msg(id="a", role="assistant", name="agent",
                 content=[TextBlock(type="text", text="v2")])
        await RS.upsert_message(env["storage"], U, SID, m2)

        key = env["storage"].key_config.messages.format(
            user_id=U, session_id=SID,
        )
        items = await env["storage"]._client.lrange(key, 0, -1)  # noqa: SLF001
        assert len(items) == 1
        import json

        assert json.loads(items[0])["content"][0]["text"] == "v2"


# ================================================================ 缺陷 3：暂停穿透


class TestPauseGate:
    async def test_pause_flag_roundtrip(self, env):
        """标志 set/clear/is_paused 基础语义。"""
        assert not await is_paused(env["storage"], U, SID)
        await set_paused(env["storage"], U, [SID])
        assert await is_paused(env["storage"], U, SID)
        await clear_paused(env["storage"], U, [SID])
        assert not await is_paused(env["storage"], U, SID)

    async def test_auto_wakeup_blocked(self, env):
        """暂停中：input=None 的自动唤醒被拦截（不进 agent 层）。"""
        await set_paused(env["storage"], U, [SID])
        await type(env["svc"]).run(env["svc"], U, SID, AGENT, None)
        assert env["rec"].calls == [], "暂停期间自动唤醒必须被拦截"

    async def test_user_msg_unpauses(self, env):
        """暂停中用户发消息：放行并清标志（用户说话永远不被拦）。"""
        await set_paused(env["storage"], U, [SID])
        user_msg = Msg(
            id="m", role="user", name="u",
            content=[TextBlock(type="text", text="你好")],
        )
        await type(env["svc"]).run(env["svc"], U, SID, AGENT, user_msg)
        assert len(env["rec"].calls) == 1, "用户消息必须放行"
        assert not await is_paused(env["storage"], U, SID), "放行后标志必须清除"

    async def test_resume_wake_passes_after_clear(self, env):
        """清标志后：自动唤醒恢复放行（resume 语义）。"""
        await set_paused(env["storage"], U, [SID])
        await clear_paused(env["storage"], U, [SID])
        await type(env["svc"]).run(env["svc"], U, SID, AGENT, None)
        assert len(env["rec"].calls) == 1, "清标志后 wake 必须放行"
