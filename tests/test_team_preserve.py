"""团队删除确认 + 软解散（培养资产保护）测试——2026-09-07 重构。

背景：官方 TeamDelete 是 LLM 自主工具 + 全灭式级联删除，与"大A+
Team 是绑定开放的产品资产"理念冲突。双层防护：
1. ``TeamDelete.check_permissions`` patch → bypass-immune ASK
   （任何权限模式下 LLM 都不能自主删团队，必须用户确认）
2. ``SessionService.delete_team`` patch → 软解散（取消成员运行 +
   清 leader team_id，成员/会话/团队记录全保留）

测试不依赖真实 Redis / 真实 LLM：
- 权限层：直接调 patch 后的 check_permissions 断言决策
- 服务层：FakeStorage + 官方 SessionService 实例（patch 后的方法）
- API 层：TestClient + FastAPI 装载 team_history_router
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from app.team_preserve import (  # noqa: E402
    patch_team_protection,
    team_history_router,
)


# ---------------------------------------------------------------- Fake 模型
@dataclass
class _TeamData:
    name: str
    members: list[dict] = field(default_factory=list)


@dataclass
class _Team:
    id: str
    session_id: str
    leader_agent_id: str
    data: _TeamData


@dataclass
class _Session:
    id: str
    team_id: str | None = None


@dataclass
class _AgentData:
    name: str


@dataclass
class _Agent:
    id: str
    data: _AgentData


class FakeStorage:
    """官方 RedisStorage 的最小 duck-typing（叠加层用到的接口）。"""

    def __init__(self):
        self.teams: dict[tuple[str, str], _Team] = {}
        self.sessions: dict[tuple[str, str], _Session] = {}
        self.agents: dict[tuple[str, str], _Agent] = {}
        self.set_team_id_calls: list[tuple[str, str, str | None]] = []

    async def get_team(self, user_id, team_id):
        return self.teams.get((user_id, team_id))

    async def set_session_team_id(self, user_id, session_id, team_id):
        self.set_team_id_calls.append((user_id, session_id, team_id))
        s = self.sessions.get((user_id, session_id))
        if s:
            s.team_id = team_id

    async def list_teams(self, user_id):
        return [t for (u, _), t in self.teams.items() if u == user_id]

    async def get_session(self, user_id, agent_id, session_id):  # noqa: ARG002
        return self.sessions.get((user_id, session_id))

    async def get_agent(self, user_id, agent_id):
        return self.agents.get((user_id, agent_id))


class FakeSessionService:
    """官方 SessionService 的最小 duck-typing（_storage + cancel）。"""

    def __init__(self, storage: FakeStorage):
        self._storage = storage
        self.cancelled: list[str] = []


async def _noop_cancel(self, session_id, **kwargs):  # noqa: ARG001, ARG002
    self.cancelled.append(session_id)
    return True


def _seed(storage: FakeStorage):
    """完整团队：leader 会话 + created/invited 两成员 + team 记录。"""
    leader_sid, ma_sid, mb_sid = "L" * 32, "A" * 32, "B" * 32
    ma_agent, mb_agent = "agent-a" + "0" * 25, "agent-b" + "0" * 25
    team_id = "T" * 32
    storage.sessions[("tester", leader_sid)] = _Session(leader_sid, team_id)
    storage.sessions[("tester", ma_sid)] = _Session(ma_sid)
    storage.sessions[("tester", mb_sid)] = _Session(mb_sid)
    storage.agents[("tester", ma_agent)] = _Agent(ma_agent, _AgentData("成员A"))
    storage.agents[("tester", mb_agent)] = _Agent(mb_agent, _AgentData("成员B"))
    storage.teams[("tester", team_id)] = _Team(
        team_id,
        leader_sid,
        "leader-agent-0001",
        _TeamData(
            "测试团队",
            [
                {
                    "owner_id": "tester",
                    "agent_id": ma_agent,
                    "session_id": ma_sid,
                    "role": "created",
                },
                {
                    "owner_id": "tester",
                    "agent_id": mb_agent,
                    "session_id": mb_sid,
                    "role": "invited",
                },
            ],
        ),
    )
    return leader_sid, ma_sid, mb_sid, ma_agent, mb_agent, team_id


@pytest.fixture(autouse=True)
def _patched():
    """挂载双层防护（幂等），并把软删除实现绑定到 Fake 服务。"""
    patch_team_protection()
    FakeSessionService.delete_team = FakeSessionService.__dict__.get(
        "_soft_delete_bound"
    ) or _bind_soft_delete()
    return None


def _bind_soft_delete():
    """从 patch 后的官方 SessionService 取实现，绑到 Fake 上复用。"""
    from agentscope.app._service._session import SessionService

    FakeSessionService.delete_team = SessionService.__dict__["delete_team"]
    FakeSessionService.cancel_session_run = _noop_cancel
    return SessionService.__dict__["delete_team"]


# ---------------------------------------------------------------- 权限层
class TestTeamDeletePermission:
    """第一层：TeamDelete 必须用户确认（bypass-immune ASK）。"""

    @pytest.mark.asyncio
    async def test_check_permissions_always_asks(self):
        from agentscope.app._tool._team_delete import TeamDelete
        from agentscope.permission import PermissionBehavior

        decision = await TeamDelete.check_permissions(None, {}, None)
        assert decision.behavior == PermissionBehavior.ASK
        assert decision.bypass_immune is True  # "始终允许"规则也压不住
        assert "团队" in decision.message

    @pytest.mark.asyncio
    async def test_decision_survives_in_modes(self):
        """决策字段完整（消息+原因），DONT_ASK 转 DENY 由引擎保证。"""
        from agentscope.app._tool._team_delete import TeamDelete

        decision = await TeamDelete.check_permissions(None, {}, None)
        assert decision.decision_reason is not None
        assert decision.message


# ---------------------------------------------------------------- 服务层
class TestSoftDeleteTeam:
    """第二层：确认后的 delete_team 是软解散（资产全保留）。"""

    @pytest.mark.asyncio
    async def test_soft_delete_preserves_everything(self):
        storage = FakeStorage()
        svc = FakeSessionService(storage)
        leader_sid, ma_sid, mb_sid, *_agents, team_id = await _seed_async(storage)

        ok = await svc.delete_team("tester", team_id)
        assert ok is True

        # team 记录保留（成员↔session 映射可追溯）
        assert ("tester", team_id) in storage.teams
        # 成员 session 保留（created 与 invited 一律不删）
        assert ("tester", ma_sid) in storage.sessions
        assert ("tester", mb_sid) in storage.sessions
        # 成员运行被取消（无僵尸运行）
        assert set(svc.cancelled) == {ma_sid, mb_sid}
        # leader session 的 team_id 已清空 → view.team 判定解散
        assert storage.sessions[("tester", leader_sid)].team_id is None

    @pytest.mark.asyncio
    async def test_missing_team_returns_false(self):
        storage = FakeStorage()
        svc = FakeSessionService(storage)
        assert await svc.delete_team("tester", "X" * 32) is False

    @pytest.mark.asyncio
    async def test_soft_delete_idempotent(self):
        storage = FakeStorage()
        svc = FakeSessionService(storage)
        leader_sid, *_ = await _seed_async(storage)
        assert await svc.delete_team("tester", "T" * 32) is True
        assert await svc.delete_team("tester", "T" * 32) is True
        assert storage.sessions[("tester", leader_sid)].team_id is None


# ---------------------------------------------------------------- API 层
class TestTeamSessionsApi:
    """GET /team-sessions/{leaderSid}：成员 session 映射查询。"""

    @pytest.fixture
    def client_and_storage(self):
        storage = FakeStorage()
        app = FastAPI()
        app.state.storage = storage
        app.include_router(team_history_router)
        return TestClient(app, headers={"X-User-ID": "tester"}), storage

    @pytest.mark.asyncio
    async def test_api_returns_member_mapping(self, client_and_storage):
        client, storage = client_and_storage
        leader_sid, ma_sid, mb_sid, ma_agent, mb_agent, team_id = await _seed_async(storage)

        resp = client.get(f"/team-sessions/{leader_sid}")
        assert resp.status_code == 200
        teams = resp.json()["teams"]
        assert len(teams) == 1
        team = teams[0]
        assert team["team_id"] == team_id
        assert team["name"] == "测试团队"
        assert team["dissolved"] is False  # 活跃团队
        by_agent = {m["agent_id"]: m for m in team["members"]}
        assert by_agent[ma_agent]["session_id"] == ma_sid
        assert by_agent[mb_agent]["session_id"] == mb_sid
        assert by_agent[ma_agent]["agent_name"] == "成员A"

    @pytest.mark.asyncio
    async def test_api_marks_dissolved_after_soft_delete(self, client_and_storage):
        client, storage = client_and_storage
        leader_sid, ma_sid, *_ = await _seed_async(storage)
        svc = FakeSessionService(storage)
        await svc.delete_team("tester", "T" * 32)

        resp = client.get(f"/team-sessions/{leader_sid}")
        team = resp.json()["teams"][0]
        assert team["dissolved"] is True
        assert team["members"][0]["session_id"] == ma_sid  # 会话映射保留

    @pytest.mark.asyncio
    async def test_api_empty_for_ordinary_session(self, client_and_storage):
        client, _ = client_and_storage
        resp = client.get("/team-sessions/" + "9" * 32)
        assert resp.status_code == 200
        assert resp.json() == {"teams": []}


async def _seed_async(storage: FakeStorage):
    """异步包装（保持与业务调用一致的 await 语义）。"""
    return _seed(storage)
