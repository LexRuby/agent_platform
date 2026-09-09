"""会话流程控制测试：暂停/继续、任意位置重新对话、流程重启（2026-09-08 v3）。

用户需求：
- 暂停/继续：停止+可继续语义（上下文完整保留），团队级暂停停全部成员
- 任意位置重新对话：截断点之后的消息归档后删除，context 同步截断
- 流程重启：上下文归零，消息历史保留

实现（app/session_flow.py，session_flow_router）：
- ``POST /sessions/{sid}/truncate``   LTRIM 消息 + 同步截断 state.context +
  归档到 ``agentforge:flow-archive:{user}:{sid}``
- ``POST /sessions/{sid}/restart``    context/summary/reply_context 归零，
  消息保留；团队 leader 先 cancel 成员
- ``POST /team-flow/{leader_sid}/pause|resume``  官方 interrupt + 成员取消 /
  enqueue wake

测试隔离原则：
- Redis → fakeredis（FakeAsyncRedis 直接注入 storage._client）
- message_bus → 官方 InMemoryMessageBus（is_locked 默认 False）
- chat_service / session_service → SimpleNamespace stub（记录调用）
- 官方 pydantic 模型构造 seed 数据（SessionRecord/TeamRecord/Msg），
  键结构取自 RedisStorage.KeyConfig 真实模板
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

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

from app.session_flow import session_flow_router  # noqa: E402

U = {"X-User-ID": "u1"}
AGENT = "a-leader"
SID = "s-1"


# ---------------------------------------------------------------- seed 工具


def _msg(mid: str, role: str = "user", text: str = "", name: str = "user") -> Msg:
    return Msg(
        id=mid,
        role=role,
        name=name,
        content=[TextBlock(type="text", text=text or f"内容-{mid}")],
    )


def _seed_session(fake, storage, *, team_id: str | None = None,
                  state: AgentState | None = None) -> SessionRecord:
    record = SessionRecord(
        user_id="u1",
        agent_id=AGENT,
        session_id=SID,
        team_id=team_id,
        config=SessionConfig(workspace_id="ws-1"),
        state=state or AgentState(session_id=SID),
    )
    key = storage.key_config.session.format(user_id="u1", session_id=SID)
    fake.set(key, record.model_dump_json())
    return record


def _seed_messages(fake, storage, msgs: list[Msg]) -> None:
    key = storage.key_config.messages.format(user_id="u1", session_id=SID)
    for m in msgs:
        fake.rpush(key, m.model_dump_json())


def _seed_team(fake, storage, *, team_id: str = "t-1",
               members: list[dict] | None = None) -> None:
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
                    agent_id=m["agent_id"],
                    session_id=m["session_id"],
                    role="created",
                )
                for m in (members or [])
            ],
        ),
    )
    key = storage.key_config.team.format(user_id="u1", team_id=team_id)
    fake.set(key, record.model_dump_json())
    fake.sadd(
        storage.key_config.team_index.format(user_id="u1"), team_id,
    )


@dataclass
class _ChatStub:
    """ChatService stub：记录 interrupt 调用。"""

    interrupts: list[tuple[str, str, str]] = field(default_factory=list)

    async def interrupt(self, user_id: str, session_id: str,
                        agent_id: str) -> None:
        self.interrupts.append((user_id, session_id, agent_id))


@dataclass
class _SessionSvcStub:
    """SessionService stub：记录 cancel_session_run / delete_team 调用。"""

    cancelled: list[str] = field(default_factory=list)
    deleted_teams: list[str] = field(default_factory=list)
    # 控制 delete_team 返回值（默认成功）
    delete_team_ok: bool = True

    async def cancel_session_run(self, session_id: str,
                                 timeout: float = 10.0) -> bool:
        self.cancelled.append(session_id)
        return True

    async def delete_team(self, user_id: str, team_id: str) -> bool:
        self.deleted_teams.append(team_id)
        return self.delete_team_ok


@pytest.fixture
def stack():
    """最小可测栈：fakeredis + 官方 storage + 真路由 + stub 服务。

    同一个 FakeServer 挂两个客户端：同步（seed 直接调用）+ 异步
    （storage._client 内部 await）。decode_responses=True 对齐官方
    RedisStorage（否则 smembers 返回 bytes，list_teams 拼 key 失配）。
    """
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    afake = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True,
    )
    storage = RedisStorage()
    storage._client = afake  # noqa: SLF001 — 绕过 __aenter__ 直接注入

    bus = InMemoryMessageBus()
    chat = _ChatStub()
    session_svc = _SessionSvcStub()

    app = FastAPI()
    app.include_router(session_flow_router)
    app.state.storage = storage
    app.state.message_bus = bus
    app.state.chat_service = chat
    app.state.session_service = session_svc
    return SimpleNamespace(
        client=TestClient(app), fake=fake, storage=storage,
        bus=bus, chat=chat, session_svc=session_svc,
    )



# ================================================================ live-status


class TestTeamLiveStatus:
    """GET /team-flow/{sid}/live-status：主理人+成员运行锁快照。

    2026-09-09 用户反馈"任务结束了还显示运行中"——前端需要区分
    「运行中」（任一会话持锁）与「休息中」（在册但全部空闲），
    判定与官方 sessions 列表同源（message_bus.is_locked）。
    """

    def test_all_idle(self, stack):
        """在册团队、全部空闲 → leader/members running 全 False。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[
                {"agent_id": "a-m1", "session_id": "s-m1"},
                {"agent_id": "a-m2", "session_id": "s-m2"},
            ],
        )

        r = stack.client.get(
            f"/team-flow/{SID}/live-status",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["leader_session_id"] == SID
        assert body["leader_running"] is False
        assert len(body["members"]) == 2
        assert all(m["running"] is False for m in body["members"])
        assert {m["session_id"] for m in body["members"]} == {"s-m1", "s-m2"}

    def test_leader_running(self, stack, monkeypatch):
        """主理人持锁 → leader_running=True。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(stack.fake, stack.storage)

        async def locked(key: str) -> bool:
            # leader 的会话锁（InMemoryMessageBus key 含 session id）
            return SID in key

        monkeypatch.setattr(stack.bus, "is_locked", locked)
        r = stack.client.get(
            f"/team-flow/{SID}/live-status",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200
        assert r.json()["leader_running"] is True

    def test_member_running(self, stack, monkeypatch):
        """成员持锁 → 该成员 running=True，其余 False。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[
                {"agent_id": "a-m1", "session_id": "s-m1"},
                {"agent_id": "a-m2", "session_id": "s-m2"},
            ],
        )

        async def locked(key: str) -> bool:
            return "s-m2" in key

        monkeypatch.setattr(stack.bus, "is_locked", locked)
        r = stack.client.get(
            f"/team-flow/{SID}/live-status",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200
        body = r.json()
        by_sid = {m["session_id"]: m["running"] for m in body["members"]}
        assert by_sid == {"s-m1": False, "s-m2": True}

    def test_no_team_empty_members(self, stack):
        """无在册团队 → members 空列表（前端判休息中的前提）。"""
        _seed_session(stack.fake, stack.storage, team_id=None)

        r = stack.client.get(
            f"/team-flow/{SID}/live-status",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200
        assert r.json()["members"] == []

    def test_unauthenticated(self, stack):
        r = stack.client.get(
            f"/team-flow/{SID}/live-status",
            params={"agent_id": AGENT},
        )
        assert r.status_code == 401


# ================================================================ dissolve


class TestDissolveTeamFlow:
    """POST /team-flow/{sid}/dissolve：用户主动解散（唯一入口）。

    2026-09-09：LLM 的 TeamDelete 已被无条件 DENY（BYPASS 也拦得住），
    解散只能由用户在团队面板发起。软解散语义：取消成员运行 + 解除
    leader 绑定，资产全保留。
    """

    def test_dissolve_with_team(self, stack):
        """在册团队 → 200，走软解散（delete_team），返回成员数。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[
                {"agent_id": "a-m1", "session_id": "s-m1"},
                {"agent_id": "a-m2", "session_id": "s-m2"},
            ],
        )

        r = stack.client.post(
            f"/team-flow/{SID}/dissolve",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == SID
        assert body["cancelled_members"] == 2
        # 软解散被调用（patched SessionService.delete_team）
        assert stack.session_svc.deleted_teams == ["t-1"]

    def test_dissolve_without_team_409(self, stack):
        """无在册团队（已解散/未组队）→ 409，且不触发删除。"""
        _seed_session(stack.fake, stack.storage, team_id=None)

        r = stack.client.post(
            f"/team-flow/{SID}/dissolve",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 409
        assert "没有在册团队" in r.json()["detail"]
        assert stack.session_svc.deleted_teams == []

    def test_dissolve_service_failure_500(self, stack):
        """软解散失败（delete_team 返回 False）→ 500。"""
        stack.session_svc.delete_team_ok = False
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(stack.fake, stack.storage)

        r = stack.client.post(
            f"/team-flow/{SID}/dissolve",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 500

    def test_dissolve_missing_session_404(self, stack):
        """会话不存在 → 404（_get_session_record 官方语义）。"""
        r = stack.client.post(
            f"/team-flow/s-none/dissolve",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 404


# ================================================================ truncate


class TestTruncate:
    """POST /sessions/{sid}/truncate：任意位置重新对话。"""

    def test_truncate_basic(self, stack):
        """5 条消息截到第 3 条：保留 3、归档 2、context 同步截断。"""
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
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m3"},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kept_messages"] == 3
        assert body["archived_messages"] == 2

        # 消息 List 只剩 3 条，且是前 3 条
        key = stack.storage.key_config.messages.format(
            user_id="u1", session_id=SID,
        )
        remaining = [
            json.loads(x)["id"]
            for x in stack.fake.lrange(key, 0, -1)
        ]
        assert remaining == ["m1", "m2", "m3"]

        # context 同步截断 + summary/reply_context 重置
        record = SessionRecord.model_validate_json(
            stack.fake.get(
                stack.storage.key_config.session.format(
                    user_id="u1", session_id=SID,
                ),
            ),
        )
        assert [m.id for m in record.state.context] == ["m1", "m2", "m3"]
        assert record.state.summary == ""
        assert record.state.reply_context.reply_id != "reply-old"

        # 归档：1 条记录、含被删的 2 条消息副本
        archive_key = (
            f"agentforge:flow-archive:u1:{SID}"
        )
        archives = [
            json.loads(x)
            for x in stack.fake.lrange(archive_key, 0, -1)
        ]
        assert len(archives) == 1
        assert archives[0]["removed_count"] == 2
        assert archives[0]["from_message_id"] == "m3"
        assert [m["id"] for m in archives[0]["messages"]] == ["m4", "m5"]

    def test_truncate_at_last_message(self, stack):
        """截断点是最后一条：无删除、不写归档。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1"), _msg("m2")])

        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m2"},
            headers=U,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "session_id": SID,
            "kept_messages": 2,
            "archived_messages": 0,
            "cancelled_members": 0,
        }
        archive_key = f"agentforge:flow-archive:u1:{SID}"
        assert stack.fake.llen(archive_key) == 0

    def test_truncate_message_not_found(self, stack):
        """截断点不存在 → 404。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "nope"},
            headers=U,
        )
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_truncate_running_conflict(self, stack, monkeypatch):
        """running（run 锁被持有）→ 409，且状态零改动。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(stack.fake, stack.storage, [_msg("m1"), _msg("m2")])

        async def locked(key: str) -> bool:
            return True

        monkeypatch.setattr(stack.bus, "is_locked", locked)
        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 409
        assert "运行中" in r.json()["detail"]

    def test_truncate_session_not_found(self, stack):
        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 404

    def test_truncate_unauthenticated(self, stack):
        _seed_session(stack.fake, stack.storage)
        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m1"},
        )
        assert r.status_code == 401

    def test_truncate_context_time_anchor(self, stack):
        """context 不含截断点消息（展示层消息）→ 时间锚点截断。

        场景：team-message 只进消息列表不进 context；按 created_at
        对齐截断位置。
        """
        anchor = _msg("m3", role="assistant", name="member-a")
        anchor.created_at = "2026-09-08T10:00:00+00:00"

        # context 里有 m1、m2（m2 晚于锚点 → 被截掉）
        c1 = _msg("m1")
        c1.created_at = "2026-09-08T09:00:00+00:00"
        c2 = _msg("m2")
        c2.created_at = "2026-09-08T11:00:00+00:00"

        state = AgentState(session_id=SID, context=[c1, c2])
        _seed_session(stack.fake, stack.storage, state=state)
        _seed_messages(stack.fake, stack.storage, [c1, anchor, c2])

        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m3"},
            headers=U,
        )
        assert r.status_code == 200, r.text
        record = SessionRecord.model_validate_json(
            stack.fake.get(
                stack.storage.key_config.session.format(
                    user_id="u1", session_id=SID,
                ),
            ),
        )
        # 时间锚点：只保留 created_at <= 锚点 的 m1
        assert [m.id for m in record.state.context] == ["m1"]

    def test_truncate_permission_preserved(self, stack):
        """permission_context / tasks_context 截断后保留。"""
        from agentscope.permission._context import AdditionalWorkingDirectory
        from agentscope.permission import PermissionMode

        state = AgentState(session_id=SID, context=[_msg("m1")])
        state.permission_context.mode = PermissionMode.BYPASS
        state.permission_context.working_directories = {
            "/tmp/allowed": AdditionalWorkingDirectory(
                path="/tmp/allowed", source="user",
            ),
        }
        _seed_session(stack.fake, stack.storage, state=state)
        _seed_messages(stack.fake, stack.storage, [_msg("m1")])

        r = stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )
        assert r.status_code == 200, r.text
        record = SessionRecord.model_validate_json(
            stack.fake.get(
                stack.storage.key_config.session.format(
                    user_id="u1", session_id=SID,
                ),
            ),
        )
        assert (
            record.state.permission_context.mode.value == "bypass"
        )
        assert "/tmp/allowed" in (
            record.state.permission_context.working_directories
        )

    def test_flow_archive_query(self, stack):
        """GET flow-archive 返回历次归档。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(
            stack.fake, stack.storage,
            [_msg("m1"), _msg("m2"), _msg("m3")],
        )
        stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m1"},
            headers=U,
        )

        r = stack.client.get(
            f"/sessions/{SID}/flow-archive",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["session_id"] == SID
        assert len(data["archives"]) == 1
        assert data["archives"][0]["removed_count"] == 2


# ================================================================ restart


class TestRestart:
    """POST /sessions/{sid}/restart：流程重启（清上下文保留历史）。"""

    def test_restart_basic(self, stack):
        """context/summary/reply 归零；消息历史完整保留。"""
        msgs = [_msg(f"m{i}") for i in range(1, 4)]
        state = AgentState(
            session_id=SID,
            context=msgs,
            summary="要被清掉的摘要",
        )
        state.reply_context = ReplyContext(reply_id="reply-old", cur_iter=5)
        _seed_session(stack.fake, stack.storage, state=state)
        _seed_messages(stack.fake, stack.storage, msgs)

        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == SID
        assert body["cancelled_members"] == 0

        record = SessionRecord.model_validate_json(
            stack.fake.get(
                stack.storage.key_config.session.format(
                    user_id="u1", session_id=SID,
                ),
            ),
        )
        assert record.state.context == []
        assert record.state.summary == ""
        assert record.state.reply_context.reply_id != "reply-old"

        # 消息历史保留
        key = stack.storage.key_config.messages.format(
            user_id="u1", session_id=SID,
        )
        assert stack.fake.llen(key) == 3

    def test_restart_running_conflict(self, stack, monkeypatch):
        _seed_session(stack.fake, stack.storage)

        async def locked(key: str) -> bool:
            return True

        monkeypatch.setattr(stack.bus, "is_locked", locked)
        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 409

    def test_restart_team_leader_cancels_members(self, stack):
        """团队 leader 重启：全部成员被 cancel + leader HITL 中断。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[
                {"agent_id": "a-m1", "session_id": "s-m1"},
                {"agent_id": "a-m2", "session_id": "s-m2"},
            ],
        )

        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cancelled_members"] == 2
        assert sorted(stack.session_svc.cancelled) == ["s-m1", "s-m2"]
        # leader 自身 interrupt（清 HITL 停靠）
        assert stack.chat.interrupts == [("u1", SID, AGENT)]

        # 团队绑定保留（重启不等于解散）
        record = SessionRecord.model_validate_json(
            stack.fake.get(
                stack.storage.key_config.session.format(
                    user_id="u1", session_id=SID,
                ),
            ),
        )
        assert record.team_id == "t-1"

    def test_restart_standalone_no_member_calls(self, stack):
        """非团队会话重启：不调 cancel、不调 interrupt。"""
        _seed_session(stack.fake, stack.storage)
        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200
        assert stack.session_svc.cancelled == []
        assert stack.chat.interrupts == []

    def test_restart_dissolved_team_skipped(self, stack):
        """已解散团队（leader team_id 已清）不触发成员 cancel。"""
        _seed_session(stack.fake, stack.storage, team_id=None)
        _seed_team(
            stack.fake, stack.storage,
            members=[{"agent_id": "a-m1", "session_id": "s-m1"}],
        )
        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200
        assert stack.session_svc.cancelled == []

    def test_restart_session_not_found(self, stack):
        r = stack.client.post(
            f"/sessions/{SID}/restart",
            json={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 404


# ================================================================ team flow


class TestTeamFlow:
    """POST /team-flow/{leader_sid}/pause|resume：团队暂停/继续。"""

    def test_pause_interrupts_leader_and_members(self, stack):
        """暂停：leader interrupt + 全部成员 cancel。"""
        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[{"agent_id": "a-m1", "session_id": "s-m1"}],
        )

        r = stack.client.post(
            f"/team-flow/{SID}/pause",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == SID
        assert body["cancelled_members"] == 1
        assert stack.session_svc.cancelled == ["s-m1"]
        assert stack.chat.interrupts == [("u1", SID, AGENT)]

    def test_pause_sets_flags_resume_clears(self, stack):
        """暂停设穿透拦截标志（leader+成员），继续清全部再 wake。

        2026-09-07 实测：暂停后官方 _notify_leader_of_failure 自动
        唤醒 leader（"暂停失效"），标志位由 chat_safety patch 拦截。
        """
        from app.chat_safety import PAUSED_KEY, is_paused

        _seed_session(stack.fake, stack.storage, team_id="t-1")
        _seed_team(
            stack.fake, stack.storage,
            members=[
                {"agent_id": "a-m1", "session_id": "s-m1"},
                {"agent_id": "a-m2", "session_id": "s-m2"},
            ],
        )

        r = stack.client.post(
            f"/team-flow/{SID}/pause",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        # leader + 两个成员的标志全部设置
        assert stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id=SID))
        assert stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id="s-m1"))
        assert stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id="s-m2"))

        r = stack.client.post(
            f"/team-flow/{SID}/resume",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        assert not stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id=SID))
        assert not stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id="s-m1"))
        assert not stack.fake.get(PAUSED_KEY.format(user_id="u1", session_id="s-m2"))

    def test_pause_standalone_leader(self, stack):
        """非团队会话（无在册团队）暂停 → 409（2026-09-09 语义更新）。

        原语义"仅中断该会话"已废弃：团队已解散/未组队时不存在
        "暂停团队"，且静默 200 会给 leader 误设暂停标志、阻止后续
        正常对话（用户反馈：设计语言一致性——按钮与状态必须对齐）。
        """
        _seed_session(stack.fake, stack.storage)
        r = stack.client.post(
            f"/team-flow/{SID}/pause",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 409
        assert "没有在册团队" in r.json()["detail"]
        # 不设置任何暂停标志、不中断
        assert stack.chat.interrupts == []
        assert stack.session_svc.cancelled == []

    def test_pause_session_not_found(self, stack):
        r = stack.client.post(
            f"/team-flow/{SID}/pause",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 404

    def test_resume_enqueues_wake(self, stack, monkeypatch):
        """继续：对 leader enqueue wake 触发（官方 input:None 语义）。"""
        _seed_session(stack.fake, stack.storage)
        calls: list[dict] = []

        async def fake_enqueue(bus, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            "app.session_flow.enqueue_run_trigger", fake_enqueue,
        )
        r = stack.client.post(
            f"/team-flow/{SID}/resume",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 200, r.text
        assert len(calls) == 1
        assert calls[0]["user_id"] == "u1"
        assert calls[0]["session_id"] == SID
        assert calls[0]["agent_id"] == AGENT
        assert calls[0]["kind"] == "wake"
        assert calls[0]["inputs"] is None

    def test_resume_session_not_found(self, stack):
        r = stack.client.post(
            f"/team-flow/{SID}/resume",
            params={"agent_id": AGENT},
            headers=U,
        )
        assert r.status_code == 404


# ================================================================ 状态语义


class TestStateSemantics:
    """跨端点的状态语义一致性（防回归）。"""

    def test_truncate_then_new_message_flows(self, stack):
        """截断后新回复的 id 与被删消息不同（upsert 不会复活旧消息）。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(
            stack.fake, stack.storage,
            [_msg("m1"), _msg("m2"), _msg("m3")],
        )
        stack.client.post(
            f"/sessions/{SID}/truncate",
            json={"agent_id": AGENT, "message_id": "m2"},
            headers=U,
        )
        # 新消息 m4 追加（模拟后续对话）
        stack.fake.rpush(
            stack.storage.key_config.messages.format(
                user_id="u1", session_id=SID,
            ),
            _msg("m4").model_dump_json(),
        )
        remaining = [
            json.loads(x)["id"]
            for x in stack.fake.lrange(
                stack.storage.key_config.messages.format(
                    user_id="u1", session_id=SID,
                ), 0, -1,
            )
        ]
        assert remaining == ["m1", "m2", "m4"]

    def test_double_truncate_idempotent(self, stack):
        """同一截断点重复调用：第二次 removed=0，消息不重复归档。"""
        _seed_session(stack.fake, stack.storage)
        _seed_messages(
            stack.fake, stack.storage,
            [_msg("m1"), _msg("m2"), _msg("m3")],
        )
        for _ in range(2):
            r = stack.client.post(
                f"/sessions/{SID}/truncate",
                json={"agent_id": AGENT, "message_id": "m2"},
                headers=U,
            )
            assert r.status_code == 200
        assert r.json()["archived_messages"] == 0
        archive_key = f"agentforge:flow-archive:u1:{SID}"
        assert stack.fake.llen(archive_key) == 1
