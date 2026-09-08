"""team_fork 端点测试：工作流节点级 fork（2026-09-08 分支对比培育）。

覆盖矩阵：
- 基础 fork：新会话消息前缀 / context 同步截断 / 原会话不动
- 团队接管：team_id 绑定新分支、team.session_id 移交、原会话显示不受影响
- 共享 workspace：分支在同一文件工作区上改进
- state 语义：summary 清空 / reply_context 重置 / 权限与任务保留
- 错误路径：会话不存在 404 / 锚点不存在 404 / 未认证 401

Redis → fakeredis（FakeAsyncRedis 注入 storage._client），与
test_session_flow 同模式。
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from agentscope.app.message_bus import InMemoryMessageBus  # noqa: E402
from agentscope.app.storage import RedisStorage  # noqa: E402
from agentscope.app.storage._model._session import (  # noqa: E402
    SessionConfig,
    SessionRecord,
)
from agentscope.app.storage._model._team import (  # noqa: E402
    TeamData,
    TeamMember,
    TeamRecord,
)
from agentscope.message import Msg, TextBlock  # noqa: E402
from agentscope.state import AgentState, ReplyContext  # noqa: E402

from app.team_fork import team_fork_router  # noqa: E402

U = {"X-User-ID": "u1"}
AGENT = "a-leader"
SID = "s-1"


# ---------------------------------------------------------------- seed 工具

def _msg(mid: str, role: str = "user", text: str = "") -> Msg:
    return Msg(
        id=mid,
        role=role,
        name=role,
        content=[TextBlock(type="text", text=text or f"内容-{mid}")],
    )


def _seed_session(fake, storage, *, team_id: str | None = None,
                  state: AgentState | None = None) -> SessionRecord:
    record = SessionRecord(
        user_id="u1",
        agent_id=AGENT,
        session_id=SID,
        team_id=team_id,
        config=SessionConfig(workspace_id="ws-1", name="原会话"),
        state=state or AgentState(session_id=SID),
    )
    key = storage.key_config.session.format(user_id="u1", session_id=SID)
    fake.set(key, record.model_dump_json())
    return record


def _seed_messages(fake, storage, msgs: list[Msg]) -> None:
    key = storage.key_config.messages.format(user_id="u1", session_id=SID)
    for m in msgs:
        fake.rpush(key, m.model_dump_json())


def _seed_team(fake, storage, *, team_id: str = "t-1") -> None:
    record = TeamRecord(
        id=team_id,
        user_id="u1",
        session_id=SID,
        leader_agent_id=AGENT,
        data=TeamData(
            name="测试团队",
            members=[
                TeamMember(
                    owner_id="u1",
                    agent_id="a-member",
                    session_id="s-member",
                    role="created",
                ),
            ],
        ),
    )
    key = storage.key_config.team.format(user_id="u1", team_id=team_id)
    fake.set(key, record.model_dump_json())
    fake.sadd(
        storage.key_config.team_index.format(user_id="u1"), team_id,
    )


def _get_session(fake, storage, sid: str) -> SessionRecord:
    return SessionRecord.model_validate_json(
        fake.get(
            storage.key_config.session.format(user_id="u1", session_id=sid),
        ),
    )


def _message_ids(fake, storage, sid: str) -> list[str]:
    key = storage.key_config.messages.format(user_id="u1", session_id=sid)
    return [json.loads(x)["id"] for x in fake.lrange(key, 0, -1)]


@pytest.fixture
def stack():
    """最小可测栈：fakeredis + 官方 storage + 真路由（同 session_flow）。"""
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    afake = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True,
    )
    storage = RedisStorage()
    storage._client = afake  # noqa: SLF001 — 绕过 __aenter__ 直接注入

    bus = InMemoryMessageBus()

    app = FastAPI()
    app.include_router(team_fork_router)
    app.state.storage = storage
    app.state.message_bus = bus
    return SimpleNamespace(
        client=TestClient(app), fake=fake, storage=storage, bus=bus,
    )


# ================================================================ 引导语
# 2026-09-08 二次确认：fork 请求可携带 initial_prompt，fork 完成后
# 作为新分支第一条用户消息自动触发 chat run（培育语义：对结果不满
# 意 → 写改进意见 → 分支带着引导重跑）。
# 测试用 mock chat_service / registry 注入 app.state，验证：
# - 带 prompt：spawn 以 (fork_sid, 引导语 Msg) 被调用，auto_started=True
# - 不带 prompt：不触发 spawn，auto_started=False
# - 空白 prompt：等价不带（strip 后不触发）
# - registry 拒绝（RuntimeError）：不 500，auto_started=False


class _FakeRegistry:
    """记录 spawn 调用的 registry 桩。"""

    def __init__(self, *, reject: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self.reject = reject

    def spawn(self, coro, session_id: str) -> None:
        self.calls.append((session_id, coro))
        coro.close()  # 不真正执行 run，避免碰 LLM
        if self.reject:
            raise RuntimeError("run already in flight")


class _FakeChatService:
    """记录 run 参数并返回可关闭协程的 chat_service 桩。"""

    def __init__(self) -> None:
        self.runs: list[dict] = []

    def run(self, *, user_id, session_id, agent_id, input_msg):
        self.runs.append({
            "user_id": user_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "input_msg": input_msg,
        })

        async def _noop() -> None:
            return None

        return _noop()


class TestForkInitialPrompt:
    """initial_prompt：fork 后引导语自动触发 chat run。"""

    def test_prompt_triggers_run(self, stack):
        """带引导语：spawn(fork_sid) 调用一次，run 收到引导语 Msg。"""
        svc, reg = _FakeChatService(), _FakeRegistry()
        stack.client.app.state.chat_service = svc
        stack.client.app.state.chat_run_registry = reg

        _seed_session(stack.fake, stack.storage)
        _seed_messages(
            stack.fake, stack.storage, [_msg("m1"), _msg("m2")],
        )

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={
                "agent_id": AGENT,
                "message_id": "m1",
                "initial_prompt": "结果未考虑非线性效应，请重新分析",
            },
            headers=U,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["auto_started"] is True
        fork_sid = body["session_id"]

        # run 以新分支会话为目标，输入是引导语用户消息
        assert len(svc.runs) == 1
        assert svc.runs[0]["session_id"] == fork_sid
        assert svc.runs[0]["agent_id"] == AGENT
        assert svc.runs[0]["user_id"] == "u1"
        msg = svc.runs[0]["input_msg"]
        assert msg.role == "user"
        assert "非线性效应" in msg.get_text_content()

        # registry 收到 spawn（session_id = 新分支）
        assert [sid for sid, _ in reg.calls] == [fork_sid]

    def test_no_prompt_no_run(self, stack):
        """不带引导语：不触发 run（保持"用户自己决定何时输入"）。"""
        svc, reg = _FakeChatService(), _FakeRegistry()
        stack.client.app.state.chat_service = svc
        stack.client.app.state.chat_run_registry = reg

        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        assert r.json()["auto_started"] is False
        assert svc.runs == []
        assert reg.calls == []

    def test_blank_prompt_treated_as_absent(self, stack):
        """纯空白引导语等价不带（strip 后不触发）。"""
        svc, reg = _FakeChatService(), _FakeRegistry()
        stack.client.app.state.chat_service = svc
        stack.client.app.state.chat_run_registry = reg

        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={
                "agent_id": AGENT,
                "message_id": "m1",
                "initial_prompt": "   \n  ",
            },
            headers=U,
        )
        assert r.status_code == 201, r.text
        assert r.json()["auto_started"] is False
        assert svc.runs == []

    def test_registry_rejection_does_not_500(self, stack):
        """registry 拒绝 spawn（单 run 防重）：fork 仍成功，防御降级。"""
        svc = _FakeChatService()
        reg = _FakeRegistry(reject=True)
        stack.client.app.state.chat_service = svc
        stack.client.app.state.chat_run_registry = reg

        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={
                "agent_id": AGENT,
                "message_id": "m1",
                "initial_prompt": "请重跑",
            },
            headers=U,
        )
        assert r.status_code == 201, r.text
        assert r.json()["auto_started"] is False  # 降级：分支已建，run 未起
        # 分支本身创建成功
        assert r.json()["kept_messages"] == 1


# ================================================================ fork 基础


class TestForkBasic:
    """POST /sessions/{sid}/team-fork：分支创建与隔离。"""

    def test_fork_basic(self, stack):
        """5 条消息 fork 到 m3：新会话 3 条、context 同步、原会话不动。"""
        msgs = [_msg(f"m{i}") for i in range(1, 6)]
        state = AgentState(
            session_id=SID,
            context=[_msg(f"m{i}") for i in range(1, 6)],
            summary="旧摘要",
        )
        state.reply_context = ReplyContext(reply_id="reply-old", cur_iter=3)
        _seed_session(stack.fake, stack.storage, state=state)
        _seed_messages(stack.fake, stack.storage, msgs)

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m3"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        fork_sid = body["session_id"]
        assert fork_sid != SID
        assert body["parent_session_id"] == SID
        assert body["kept_messages"] == 3
        assert body["team_taken_over"] is False  # 无团队

        # 新分支：消息=前 3 条、context 同步截断、state 重置
        assert _message_ids(stack.fake, stack.storage, fork_sid) == [
            "m1", "m2", "m3",
        ]
        fork_record = _get_session(stack.fake, stack.storage, fork_sid)
        assert [m.id for m in fork_record.state.context] == [
            "m1", "m2", "m3",
        ]
        assert fork_record.state.summary == ""
        assert fork_record.state.reply_context.reply_id != "reply-old"

        # 原会话：5 条消息、context、summary 全部不动（旧结果保留）
        assert _message_ids(stack.fake, stack.storage, SID) == [
            "m1", "m2", "m3", "m4", "m5",
        ]
        orig = _get_session(stack.fake, stack.storage, SID)
        assert len(orig.state.context) == 5
        assert orig.state.summary == "旧摘要"

    def test_fork_shared_workspace(self, stack):
        """分支共享原 workspace（在既有代码成果上改进，不从空目录重来）。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        fork_sid = r.json()["session_id"]
        fork_record = _get_session(stack.fake, stack.storage, fork_sid)
        assert fork_record.config.workspace_id == "ws-1"
        # 分支名带「分支」标记（会话列表可辨识血缘）
        assert "分支" in (fork_record.config.name or "")

    def test_fork_state_semantics(self, stack):
        """state 截断语义：summary/reply/middle 清空，权限任务保留。"""
        state = AgentState(
            session_id=SID,
            context=[_msg("m1"), _msg("m2")],
            summary="要被清掉的摘要",
            middle_context={"mw-key": "val"},
        )
        state.reply_context = ReplyContext(reply_id="r-old", cur_iter=2)
        from agentscope.state import TaskContext
        from agentscope.state._task import Task
        from agentscope.permission._rule import PermissionRule
        state.permission_context.allow_rules = {
            "bash": [PermissionRule(
                tool_name="bash", rule_content="*",
                behavior="allow", source="user",
            )],
        }
        state.tasks_context = TaskContext(
            tasks=[Task(
                id="t1", subject="任务", description="",
                metadata={}, created_at="2026-09-08T00:00:00",
            )],
        )
        _seed_session(stack.fake, stack.storage, state=state)
        _seed_messages(
            stack.fake, stack.storage, [_msg("m1"), _msg("m2")],
        )

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m2"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        fork_sid = r.json()["session_id"]
        st = _get_session(stack.fake, stack.storage, fork_sid).state
        assert st.summary == ""
        assert st.reply_context.reply_id != "r-old"
        assert st.middle_context == {}
        # 已授权工具与任务清单保留（培育资产不丢）
        assert "bash" in st.permission_context.allow_rules  # 授权保留
        assert st.tasks_context.tasks[0].id == "t1"  # 任务清单保留

    def test_fork_custom_name(self, stack):
        """自定义分支名优先于默认生成。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={
                "agent_id": AGENT,
                "message_id": "m1",
                "name": "我的实验分支",
            },
            headers=U,
        )
        assert r.status_code == 201, r.text
        fork_sid = r.json()["session_id"]
        assert (
            _get_session(stack.fake, stack.storage, fork_sid).config.name
            == "我的实验分支"
        )


# ================================================================ 团队接管


class TestForkTeamTakeover:
    """fork 时团队调度权移交：TeamSay directory 校验决定单 leader 会话。"""

    def test_fork_takes_over_team(self, stack):
        """有团队时：分支绑定团队、team.session_id 移交、原会话保留视图。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_messages(
            stack.fake, stack.storage, [_msg("m1"), _msg("m2")],
        )
        _seed_team(stack.fake, stack.storage)

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m2"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        fork_sid = body["session_id"]
        assert body["team_taken_over"] is True

        # 分支会话绑定团队
        fork_record = _get_session(stack.fake, stack.storage, fork_sid)
        assert fork_record.team_id == "t-1"

        # team 的活跃 leader 指针移交（成员汇报将路由到新分支）
        team_key = stack.storage.key_config.team.format(
            user_id="u1", team_id="t-1",
        )
        team = TeamRecord.model_validate_json(
            stack.fake.get(team_key),
        )
        assert team.session_id == fork_sid

        # 原会话 record.team_id 保留 → 前端 team 视图（按
        # session.team_id 查询）不受影响，仍可回看对比
        orig = _get_session(stack.fake, stack.storage, SID)
        assert orig.team_id == "t-1"

    def test_fork_team_record_missing(self, stack):
        """team_id 残留但 team 记录缺失：分支退化为普通会话（不 500）。"""
        _seed_session(stack.fake, stack.storage, team_id="t-gone")
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])
        # 不 seed team → get_team 返回 None

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["team_taken_over"] is False
        fork_record = _get_session(
            stack.fake, stack.storage, body["session_id"],
        )
        assert fork_record.team_id is None


# ================================================================ 错误路径


class TestForkErrors:
    """fork 错误路径与边界。"""

    def test_fork_session_not_found(self, stack):
        """会话不存在 → 404。"""
        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 404

    def test_fork_message_not_found(self, stack):
        """锚点消息不存在 → 404。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m999"},
            headers=U,
        )
        assert r.status_code == 404

    def test_fork_unauthenticated(self, stack):
        """未认证 → 401。"""
        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
        )
        assert r.status_code == 401

    def test_fork_other_users_session_invisible(self, stack):
        """他人会话对当前用户不可见 → 404（按 user_id 键控隔离）。"""
        _seed_session(stack.fake, stack.storage)

        r = stack.client.post(
            f"/sessions/{SID}/team-fork",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers={"X-User-ID": "u2"},
        )
        assert r.status_code == 404
