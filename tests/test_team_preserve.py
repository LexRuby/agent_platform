"""团队软解散（培养资产保留）测试——2026-09-07 培养能力重构。

背景：官方 TeamDelete 全灭式级联删除（成员 agent + 成员 session +
team 记录），用户点"进入会话迭代"后看不到成员在团队任务中的任何
内容。patch 后：解散只解除 leader session 的 team_id 绑定，成员/
会话/团队记录全部保留，并可通过 GET /team-sessions/{leaderSid}
追溯成员 session 映射（跳到成员团队会话继续介入培养）。

测试聚焦叠加层自身逻辑：用最小 FakeStorage（ duck-typing 官方
RedisStorage 的 get_team / set_session_team_id / list_teams /
get_session / get_agent 接口）验证 patch 行为与 API，不依赖真实
Redis，也不重复测官方存储内部。
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from app.team_preserve import patch_delete_team, team_history_router  # noqa: E402


@dataclass
class _TeamData:
    """官方 TeamRecord.data 的形状（name + members）。"""
    name: str
    members: list[dict] = field(default_factory=list)


@dataclass
class _Team:
    """官方 TeamRecord 的形状（data 嵌套）。"""
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
    """官方 RedisStorage 的最小 duck-typing（仅叠加层用到的接口）。"""

    def __init__(self):
        self.teams: dict[tuple[str, str], _Team] = {}
        self.sessions: dict[tuple[str, str], _Session] = {}
        self.agents: dict[tuple[str, str], _Agent] = {}
        self.set_team_id_calls: list[tuple[str, str, str | None]] = []
        self.deleted_members: list[str] = []  # 官方硬删才会写入（patch 后不应出现）

    async def get_team(self, user_id, team_id):
        return self.teams.get((user_id, team_id))

    async def set_session_team_id(self, user_id, session_id, team_id):
        self.set_team_id_calls.append((user_id, session_id, team_id))
        s = self.sessions.get((user_id, session_id))
        if s:
            s.team_id = team_id

    async def list_teams(self, user_id):
        return [t for (u, _), t in self.teams.items() if u == user_id]

    async def get_session(self, user_id, agent_id, session_id):
        return self.sessions.get((user_id, session_id))

    async def get_agent(self, user_id, agent_id):
        return self.agents.get((user_id, agent_id))


def _seed(storage: FakeStorage):
    """造一个完整团队：leader 会话 + 2 成员（created/invited）+ team 记录。"""
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
                {"owner_id": "tester", "agent_id": ma_agent, "session_id": ma_sid, "role": "created"},
                {"owner_id": "tester", "agent_id": mb_agent, "session_id": mb_sid, "role": "invited"},
            ],
        ),
    )
    return leader_sid, ma_sid, mb_sid, ma_agent, mb_agent, team_id


@pytest.fixture(autouse=True)
def soft_delete():
    """patch 官方类，并把软删除实现绑定到 FakeStorage（复用同一逻辑）。"""
    fn = patch_delete_team()
    FakeStorage.delete_team = fn
    return fn


class TestSoftDeleteTeam:
    """patch 后的 delete_team：保留培养资产，只解除团队绑定。"""

    @pytest.mark.asyncio
    async def test_soft_delete_preserves_everything(self):
        """解散后：team 记录在、成员会话在、leader team_id 清空。"""
        storage = FakeStorage()
        leader_sid, ma_sid, mb_sid, *_agent_ids, team_id = await _async_seed(storage)

        ok = await storage.delete_team("tester", team_id)
        assert ok is True

        # team 记录保留（成员↔session 映射可追溯）
        assert ("tester", team_id) in storage.teams
        # 成员 session 保留（created 与 invited 一律不删）
        assert ("tester", ma_sid) in storage.sessions
        assert ("tester", mb_sid) in storage.sessions
        # leader session 的 team_id 已清空 → view.team 判定解散
        assert storage.sessions[("tester", leader_sid)].team_id is None
        # 调用了官方解绑接口（而非删除）
        assert ("tester", leader_sid, None) in storage.set_team_id_calls

    @pytest.mark.asyncio
    async def test_missing_team_returns_false(self):
        """team 不存在 → False（官方语义保留给已清理的旧数据）。"""
        storage = FakeStorage()
        assert await storage.delete_team("tester", "X" * 32) is False

    @pytest.mark.asyncio
    async def test_soft_delete_idempotent(self):
        """二次解散幂等安全。"""
        storage = FakeStorage()
        leader_sid, *_ = await _async_seed(storage)
        team_id = "T" * 32
        assert await storage.delete_team("tester", team_id) is True
        assert await storage.delete_team("tester", team_id) is True
        assert storage.sessions[("tester", leader_sid)].team_id is None


class TestTeamSessionsApi:
    """GET /team-sessions/{leaderSessionId}：成员 session 映射查询。"""

    @pytest.fixture
    def client_and_storage(self):
        storage = FakeStorage()
        app = FastAPI()
        app.state.storage = storage
        app.include_router(team_history_router)
        return TestClient(app, headers={"X-User-ID": "tester"}), storage

    @pytest.mark.asyncio
    async def test_api_returns_member_mapping(self, client_and_storage):
        """返回团队名、解散状态与成员 agent↔session 映射。"""
        client, storage = client_and_storage
        leader_sid, ma_sid, mb_sid, ma_agent, mb_agent, team_id = await _async_seed(storage)

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
        """软解散后同一接口标 dissolved=true（会话映射仍可追溯）。"""
        client, storage = client_and_storage
        leader_sid, ma_sid, *_ = await _async_seed(storage)
        await storage.delete_team("tester", "T" * 32)

        resp = client.get(f"/team-sessions/{leader_sid}")
        team = resp.json()["teams"][0]
        assert team["dissolved"] is True
        assert team["members"][0]["session_id"] == ma_sid

    @pytest.mark.asyncio
    async def test_api_empty_for_ordinary_session(self, client_and_storage):
        """普通会话（无团队）返回空列表。"""
        client, _ = client_and_storage
        resp = client.get("/team-sessions/" + "9" * 32)
        assert resp.status_code == 200
        assert resp.json() == {"teams": []}


async def _async_seed(storage: FakeStorage):
    """异步包装（保持与业务调用一致的 await 语义）。"""
    return _seed(storage)
