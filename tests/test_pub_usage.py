"""发布物使用统计（发布者视角）测试——2026-09-09 培育闭环数据回顾。

背景：/usage/summary 是消费者视角（查自己消费多少）。所有者要的是
**发布者视角**——"产品被谁用了、跑了多少任务、token 烧在哪"，
迭代决策的输入（和 agent 对话 + 使用数据回顾双驱动）。

实现（app/agent_share.py GET /agent-share/pubs/usage）：
- 跨全平台用户扫 ``agentforge:usage:{user}``（跳过 seen 去重 Set）
- 归属：产品本体 field + 产品在该用户账号里领导的团队成员 field
  （动态组队成员归入产品，「大A及团队」口径）
- 输出：active_users / totals / by_date / by_model / members（按名
  跨用户聚合——图纸保证同名，发布者看角色维度成本）

测试不依赖真实 Redis / 真实 LLM：
- Redis → fakeredis（scan_iter + hgetall 真实语义）
- storage → FakeStorage + list_teams 注入
- 团队 → dataclass 假 Team（leader_agent_id + data.members）
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from app.agent_share import agent_share_router  # noqa: E402
from app.usage_metering import _USAGE_KEY  # noqa: E402


# ---------------------------------------------------------------- Fake 模型

@dataclass
class _TeamData:
    members: list[dict] = field(default_factory=list)


@dataclass
class _Team:
    leader_agent_id: str
    data: _TeamData = None

    def __post_init__(self):
        if self.data is None:
            self.data = _TeamData()


class FakeStorage:
    """官方 storage 子集：_client（fakeredis）+ list_teams。"""

    def __init__(self):
        self._client = fakeredis.FakeAsyncRedis(decode_responses=True)
        self.teams_by_user: dict[str, list[_Team]] = {}

    async def list_teams(self, user_id: str) -> list[_Team]:
        return self.teams_by_user.get(user_id, [])


# ---------------------------------------------------------------- 辅助

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=n)
    ).strftime("%Y-%m-%d")


def _seed_usage(client, user: str, entries: dict) -> None:
    """直写 usage hash：{field: {in,out,cache,calls,agent_name}}。"""
    for f, v in entries.items():
        asyncio.run(
            client.hset(_USAGE_KEY.format(user=user), f, json.dumps(v))
        )


def _seed_publication(client, owner: str, pub_id: str, name: str,
                      version: int = 1, team_mode: str = "blueprint") -> None:
    asyncio.run(client.sadd(f"agentforge:share:pubs:{owner}", pub_id))
    asyncio.run(client.set(
        f"agentforge:share:pubmeta:{pub_id}",
        json.dumps({
            "source_agent_id": "src-1", "source_version": version,
            "display_name": name, "published_at": "2026-09-09T10:00:00Z",
            "team_mode": team_mode,
        }, ensure_ascii=False),
    ))


def _team(leader: str, member_ids: list[str]) -> _Team:
    return _Team(leader, _TeamData([{"agent_id": m} for m in member_ids]))


@pytest.fixture
def api_app():
    app = FastAPI()
    app.include_router(agent_share_router)
    return app


def _usage(client: TestClient, user: str, days: int = 30) -> dict:
    return client.get(
        "/agent-share/pubs/usage",
        params={"days": days},
        headers={"X-User-ID": user},
    )


# ---------------------------------------------------------------- 用例

class TestPubsUsage:
    """发布者视角聚合：跨用户、动态成员归属、隐私边界、窗口过滤。"""

    def test_cross_user_aggregation_with_team_members(self, api_app):
        """两个用户各用产品组队 → 整体消耗聚合 + 成员按名合并 + 活跃 2 人。

        「大A及团队」口径：主理人本体 + 各用户账号里重建的团队成员
        全部归入发布产品（调用一次任务的完整成本）。
        """
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "机械控制实验室 v3")

        # alice 自己（发布者测试）+ bob 都用了产品
        _seed_usage(storage._client, "alice", {
            f"{_today()}|pub-1|glm-4.7": {"in": 500, "out": 200, "cache": 0, "calls": 2, "agent_name": "机械控制实验室 v3"},
        })
        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 300, "out": 100, "cache": 0, "calls": 1, "agent_name": "机械控制实验室 v3"},
            f"{_today()}|bob-m1|glm-4.7": {"in": 80, "out": 40, "cache": 0, "calls": 1, "agent_name": "robot_dynamicist"},
            f"{_today()}|bob-m2|glm-4.7": {"in": 60, "out": 30, "cache": 0, "calls": 1, "agent_name": "sim_engineer"},
        })
        # bob 账号里：产品领导的团队（动态重建的成员）
        storage.teams_by_user["bob"] = [_team("pub-1", ["bob-m1", "bob-m2"])]

        r = _usage(TestClient(api_app), "alice")
        assert r.status_code == 200, r.text
        pubs = r.json()["publications"]
        assert len(pubs) == 1
        p = pubs[0]
        assert p["agent_id"] == "pub-1"
        assert p["display_name"] == "机械控制实验室 v3"
        assert p["active_users"] == 2
        # 总量 = alice(500+200) + bob 产品(300+100) + bob 成员(80+60 + 40+30)
        assert p["totals"]["in"] == 940
        assert p["totals"]["out"] == 370
        assert p["totals"]["calls"] == 5
        # 成员构成按名聚合（bob 的两个成员 + 产品本体）
        names = {m["name"]: m for m in p["members"]}
        assert set(names) == {"机械控制实验室 v3", "robot_dynamicist", "sim_engineer"}
        assert names["robot_dynamicist"]["in"] == 80
        # 模型拆分
        assert p["by_model"][0]["model"] == "glm-4.7"
        assert p["by_model"][0]["in"] == 940

    def test_members_merged_across_users_by_name(self, api_app):
        """不同用户重建的同名成员合并统计（发布者看角色维度成本）。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")

        _seed_usage(storage._client, "bob", {
            f"{_today()}|b-m1|glm-4.7": {"in": 100, "out": 50, "cache": 0, "calls": 1, "agent_name": "robot_dynamicist"},
        })
        _seed_usage(storage._client, "carol", {
            f"{_today()}|c-m1|glm-4.7": {"in": 300, "out": 150, "cache": 0, "calls": 2, "agent_name": "robot_dynamicist"},
        })
        storage.teams_by_user["bob"] = [_team("pub-1", ["b-m1"])]
        storage.teams_by_user["carol"] = [_team("pub-1", ["c-m1"])]

        pubs = _usage(TestClient(api_app), "alice").json()["publications"]
        members = pubs[0]["members"]
        assert len(members) == 1, "同名角色跨用户必须合并"
        assert members[0]["name"] == "robot_dynamicist"
        assert members[0]["in"] == 400
        assert members[0]["calls"] == 3
        assert pubs[0]["active_users"] == 2

    def test_unrelated_agents_excluded(self, api_app):
        """用户账号里其他 agent（别的 leader 的成员/独立 agent）不混入。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")

        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 100, "out": 50, "cache": 0, "calls": 1, "agent_name": "产品"},
            f"{_today()}|other-leader|glm-4.7": {"in": 999, "out": 999, "cache": 0, "calls": 9, "agent_name": "别的大A"},
            f"{_today()}|stranger-m|glm-4.7": {"in": 500, "out": 500, "cache": 0, "calls": 5, "agent_name": "别人的成员"},
        })
        # bob 账号里 stranger-m 属于 other-leader（不是 pub-1 的团队）
        storage.teams_by_user["bob"] = [_team("other-leader", ["stranger-m"])]

        pubs = _usage(TestClient(api_app), "alice").json()["publications"]
        assert pubs[0]["totals"]["in"] == 100
        assert pubs[0]["totals"]["calls"] == 1

    def test_non_publisher_gets_empty(self, api_app):
        """非发布者（无发布物）→ 空列表，不泄漏他人数据。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")
        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 100, "out": 50, "cache": 0, "calls": 1, "agent_name": "产品"},
        })

        r = _usage(TestClient(api_app), "bob")
        assert r.status_code == 200
        assert r.json()["publications"] == []

    def test_days_window_filtering(self, api_app):
        """窗口外的用量排除（与 /usage/summary 一致的天级粒度）。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")

        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 100, "out": 50, "cache": 0, "calls": 1, "agent_name": "产品"},
            f"{_days_ago(10)}|pub-1|glm-4.7": {"in": 700, "out": 300, "cache": 0, "calls": 3, "agent_name": "产品"},
            f"{_days_ago(40)}|pub-1|glm-4.7": {"in": 999, "out": 999, "cache": 0, "calls": 9, "agent_name": "产品"},
        })

        pubs = _usage(TestClient(api_app), "alice", days=7).json()["publications"]
        assert pubs[0]["totals"]["in"] == 100  # 仅今天
        assert pubs[0]["active_users"] == 1

    def test_seen_dedup_set_skipped(self, api_app):
        """agentforge:usage:seen（Set）匹配扫描模式但不是 Hash——必须跳过。

        不跳过会在 hgetall 上抛 WRONGTYPE，整个端点 500。
        """
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")
        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 10, "out": 5, "cache": 0, "calls": 1, "agent_name": "产品"},
        })
        # 模拟生产：计量去重 Set 恰好存在
        asyncio.run(storage._client.sadd("agentforge:usage:seen", "msg-1"))

        r = _usage(TestClient(api_app), "alice")
        assert r.status_code == 200, r.text
        assert r.json()["publications"][0]["totals"]["calls"] == 1

    def test_by_date_trend(self, api_app):
        """按日趋势：多日数据各自成行，日期降序。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "产品")

        _seed_usage(storage._client, "bob", {
            f"{_today()}|pub-1|glm-4.7": {"in": 100, "out": 50, "cache": 0, "calls": 1, "agent_name": "产品"},
            f"{_days_ago(1)}|pub-1|glm-4.7": {"in": 200, "out": 100, "cache": 0, "calls": 2, "agent_name": "产品"},
        })

        pubs = _usage(TestClient(api_app), "alice").json()["publications"]
        by_date = pubs[0]["by_date"]
        assert [d["date"] for d in by_date] == sorted(
            (d["date"] for d in by_date), reverse=True,
        )
        dates = {d["date"]: d for d in by_date}
        assert dates[_days_ago(1)]["in"] == 200

    def test_team_mode_metadata_echoed(self, api_app):
        """发布物统计回显团队形态（blueprint/auto）。"""
        storage = FakeStorage()
        api_app.state.storage = storage
        _seed_publication(storage._client, "alice", "pub-1", "固定版")
        _seed_publication(
            storage._client, "alice", "pub-2", "自动版", team_mode="auto",
        )

        pubs = _usage(TestClient(api_app), "alice").json()["publications"]
        modes = {p["agent_id"]: p["team_mode"] for p in pubs}
        assert modes == {"pub-1": "blueprint", "pub-2": "auto"}

    def test_unauthenticated_401(self, api_app):
        """无身份头 → 401。"""
        api_app.state.storage = FakeStorage()
        client = TestClient(api_app)
        assert client.get("/agent-share/pubs/usage").status_code == 401

    def test_days_param_validation(self, api_app):
        """days 越界（0/366）→ 422。"""
        api_app.state.storage = FakeStorage()
        client = TestClient(api_app)
        assert client.get(
            "/agent-share/pubs/usage", params={"days": 0},
            headers={"X-User-ID": "alice"},
        ).status_code == 422
        assert client.get(
            "/agent-share/pubs/usage", params={"days": 366},
            headers={"X-User-ID": "alice"},
        ).status_code == 422
