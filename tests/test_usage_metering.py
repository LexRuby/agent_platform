"""Token 用量计量与统计测试——消费计量 v1（2026-09-07）。

背景：SaaS 前置能力，用户要知道"我这个账号的消耗情况——模型、
大A/小A、输入、输出"。实现分两层（app/usage_metering.py）：
1. 实时钩子：patch ``upsert_message``，usage 有值且消息 id 未见过
   才计量（seen Set 去重，防流式 lset 重复累计）
2. 存量回填：``backfill_usage`` 扫全部会话消息（幂等）
3. 查询 API：``GET /usage/summary`` 按 user_id 隔离，聚合出
   总计/按日期/按 agent/按模型四个维度

测试不依赖真实 Redis / 真实 LLM：
- Redis → fakeredis（FakeAsyncRedis）
- storage → 每个 fixture 现场定义新类（patch 的是 *类* 方法，
  复用同一个类会让第二次 patch 的 original 指向第一次的钩子，
  消息被双重计量）
- 消息 → dataclass 假 Msg（id/usage/name/created_at）
- API → TestClient + 装载 usage_router，X-User-ID 直接注入头
  （生产由 AuthMiddleware 覆盖写入，鉴权本身另有 test_auth_api）
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agentforge"))

from app.usage_metering import (  # noqa: E402
    _USAGE_KEY,
    backfill_usage,
    patch_usage_metering,
    usage_router,
)


# ---------------------------------------------------------------- Fake 模型

@dataclass
class FakeUsage:
    """官方 Msg.usage 的形状子集。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_input_tokens: int = 0


@dataclass
class FakeMsg:
    """官方 Msg 的形状子集（计量只读这些属性）。"""

    id: str
    name: str = ""
    usage: FakeUsage | None = None
    created_at: str = ""
    finished_at: str = ""


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def make_storage():
    """工厂：每次产出**新类**的 FakeStorage，隔离 patch 的类方法替换。

    官方 RedisStorage 暴露给计量层的接口子集：
    - ``upsert_message(user_id, session_id, msg)``：patch 目标
    - ``list_messages(user_id, session_id, limit)``：回填扫描
    - ``_client``：fakeredis（计量钩子直读写键）
    """

    def _make():
        class FakeStorage:
            def __init__(self) -> None:
                self._client = fakeredis.FakeAsyncRedis(decode_responses=True)
                # (user_id, session_id) -> list[Msg]，模拟官方消息列表
                self.buckets: dict[tuple[str, str], list] = {}

            async def upsert_message(self, user_id: str, session_id: str, msg) -> None:
                bucket = self.buckets.setdefault((user_id, session_id), [])
                # 官方语义：同 id 流式更新是 lset 覆盖，不是追加
                for i, m in enumerate(bucket):
                    if m.id == msg.id:
                        bucket[i] = msg
                        return
                bucket.append(msg)

            async def list_messages(self, user_id: str, session_id: str, limit: int = 100):
                msgs = self.buckets.get((user_id, session_id), [])
                return msgs[:limit], len(msgs) > limit

        return FakeStorage()

    return _make


def _seed_session(storage, user_id: str, session_id: str, agent_id: str, model: str) -> None:
    """直写官方会话键（计量钩子靠它归因 agent/模型）。"""
    import asyncio

    asyncio.run(
        storage._client.set(
            f"agentscope:user:{user_id}:session:{session_id}",
            json.dumps(
                {"agent_id": agent_id, "config": {"chat_model_config": {"model": model}}}
            ),
        )
    )


@pytest.fixture
def api_app():
    """装载 usage_router 的最小 app；storage 由用例挂到 state。"""
    app = FastAPI()
    app.include_router(usage_router)
    return app


def _summary(client: TestClient, user: str, days: int = 30) -> dict:
    res = client.get(
        "/usage/summary", params={"days": days}, headers={"X-User-ID": user}
    )
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- 实时钩子

class TestRealtimeMetering:
    """patch 后的 upsert_message：计量、去重、归因、容错。"""

    def test_counts_usage_with_agent_and_model_attribution(self, make_storage):
        """带 usage 的 assistant 消息 → 按 (今日, agent, 模型) 落一条聚合。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "leader-1", "glm-4.7")

        msg = FakeMsg(
            id="m1", name="主理人（大A）",
            usage=FakeUsage(input_tokens=100, output_tokens=50, cache_input_tokens=10),
        )
        asyncio.run(storage.upsert_message("u1", "s1", msg))

        key = _USAGE_KEY.format(user="u1")
        raw = asyncio.run(storage._client.hgetall(key))
        assert len(raw) == 1
        date, agent_id, model = next(iter(raw)).split("|", 2)
        assert date == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert agent_id == "leader-1"
        assert model == "glm-4.7"
        val = json.loads(next(iter(raw.values())))
        assert val["in"] == 100
        assert val["out"] == 50
        assert val["cache"] == 10
        assert val["calls"] == 1
        assert val["agent_name"] == "主理人（大A）"

    def test_stream_update_same_id_deduped(self, make_storage):
        """流式回复对同 id 多次 upsert（usage 从空到终值）→ 只计一次终值。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "a1", "m1")

        # 首写：usage 为空（流式未完成）——不计
        asyncio.run(storage.upsert_message("u1", "s1", FakeMsg(id="m1", name="x")))
        # 中间更新：部分 usage——计一次
        asyncio.run(
            storage.upsert_message(
                "u1", "s1", FakeMsg(id="m1", name="x", usage=FakeUsage(10, 5))
            )
        )
        # 终值更新：usage 变大——不得重复累计
        asyncio.run(
            storage.upsert_message(
                "u1", "s1",
                FakeMsg(id="m1", name="x", usage=FakeUsage(100, 50, 5)),
            )
        )

        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        assert len(raw) == 1
        val = json.loads(next(iter(raw.values())))
        assert (val["in"], val["out"], val["cache"], val["calls"]) == (10, 5, 0, 1), (
            "同 id 的后续 upsert 必须被 seen Set 去重（首次有值即定格）"
        )

    def test_empty_usage_never_counted(self, make_storage):
        """用户消息/无 usage 消息不产生计量数据。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "a1", "m1")

        asyncio.run(storage.upsert_message("u1", "s1", FakeMsg(id="u1", name="user")))
        asyncio.run(
            storage.upsert_message(
                "u1", "s1", FakeMsg(id="u2", usage=FakeUsage(0, 0, 0))
            )
        )

        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        assert raw == {}, "空 usage 不应产生任何计量记录"

    def test_missing_id_not_counted(self, make_storage):
        """无 id 的消息（防御分支）不计。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "a1", "m1")

        asyncio.run(
            storage.upsert_message(
                "u1", "s1", FakeMsg(id="", usage=FakeUsage(1, 1))
            )
        )
        assert asyncio.run(
            storage._client.exists(_USAGE_KEY.format(user="u1"))
        ) == 0

    def test_separate_fields_per_agent_and_model(self, make_storage):
        """不同 (agent, 模型) 的消耗分 field 聚合，互不合并。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s-leader", "leader-1", "glm-4.7")
        _seed_session(storage, "u1", "s-member", "member-7", "doubao-pro")

        asyncio.run(
            storage.upsert_message(
                "u1", "s-leader", FakeMsg(id="m1", name="大A", usage=FakeUsage(10, 20))
            )
        )
        asyncio.run(
            storage.upsert_message(
                "u1", "s-member", FakeMsg(id="m2", name="政策研究员", usage=FakeUsage(1, 2))
            )
        )
        # 同 session 再来一条 → 同 field 累加 calls
        asyncio.run(
            storage.upsert_message(
                "u1", "s-leader", FakeMsg(id="m3", name="大A", usage=FakeUsage(5, 5))
            )
        )

        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        assert len(raw) == 2, "两个 (agent, 模型) 组合应各自一条 field"
        fields = {f.split("|", 1)[1]: json.loads(v) for f, v in raw.items()}
        assert fields["leader-1|glm-4.7"] == {
            "in": 15, "out": 25, "cache": 0, "calls": 2, "agent_name": "大A",
        }
        assert fields["member-7|doubao-pro"]["calls"] == 1

    def test_user_isolation(self, make_storage):
        """用户 A 的消耗绝不写进用户 B 的键（多租户隔离）。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "alice", "s1", "a1", "m1")
        _seed_session(storage, "bob", "s1", "a1", "m1")

        asyncio.run(
            storage.upsert_message(
                "alice", "s1", FakeMsg(id="m1", usage=FakeUsage(7, 3))
            )
        )
        assert asyncio.run(
            storage._client.exists(_USAGE_KEY.format(user="bob"))
        ) == 0, "alice 的消息不得写入 bob 的用量键"
        assert asyncio.run(
            storage._client.exists(_USAGE_KEY.format(user="alice"))
        ) == 1

    def test_unknown_session_falls_back(self, make_storage):
        """会话键缺失（异常/竞态）→ 归因 unknown 而非丢弃或报错。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        # 不 seed 会话键——_resolve_session 拿不到
        asyncio.run(
            storage.upsert_message(
                "u1", "ghost", FakeMsg(id="m1", name="x", usage=FakeUsage(1, 1))
            )
        )
        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        assert len(raw) == 1
        field = next(iter(raw))
        _, agent_id, model = field.split("|", 2)
        assert agent_id == "unknown"
        assert model == "unknown"

    def test_metering_failure_does_not_block_upsert(self, make_storage, monkeypatch):
        """计量内部异常必须被吞掉——消息落库永远优先（可用性 > 统计）。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "a1", "m1")

        async def boom(*_args, **_kwargs):
            raise RuntimeError("redis down")

        # seen 判定就炸——钩子 try 块应兜住
        monkeypatch.setattr(storage._client, "sismember", boom)

        msg = FakeMsg(id="m1", usage=FakeUsage(1, 1))
        asyncio.run(storage.upsert_message("u1", "s1", msg))  # 不得抛
        assert storage.buckets[("u1", "s1")] == [msg], "消息本体必须已落库"


# ---------------------------------------------------------------- 存量回填

class TestBackfill:
    """backfill_usage：扫存量、幂等、日期归因、用户发现。"""

    def test_backfill_counts_existing_messages(self, make_storage):
        """启动回填把存量会话里的 usage 消息计入统计。"""
        import asyncio

        storage = make_storage()
        _seed_session(storage, "u1", "s1", "leader-1", "glm-4.7")
        storage.buckets[("u1", "s1")] = [
            FakeMsg(id="m1", name="大A", usage=FakeUsage(100, 200),
                    created_at=_iso_days_ago(1)),
            FakeMsg(id="m2", usage=FakeUsage(1, 2), created_at=_iso_days_ago(1)),
            FakeMsg(id="m3"),  # 无 usage：跳过
        ]

        metered = asyncio.run(backfill_usage(storage, user_ids=["u1"]))
        assert metered == 2

        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        assert len(raw) == 1
        date, agent_id, model = next(iter(raw)).split("|", 2)
        assert date == _iso_days_ago(1)[:10], "回填按消息时间归日期，而非今天"
        assert agent_id == "leader-1"
        assert model == "glm-4.7"
        val = json.loads(next(iter(raw.values())))
        assert val["in"] == 101 and val["out"] == 202 and val["calls"] == 2

    def test_backfill_idempotent(self, make_storage):
        """重复回填不得重复累计（seen Set 挡住）。"""
        import asyncio

        storage = make_storage()
        _seed_session(storage, "u1", "s1", "a1", "m1")
        storage.buckets[("u1", "s1")] = [
            FakeMsg(id="m1", usage=FakeUsage(10, 10), created_at=_iso_days_ago(0))
        ]

        assert asyncio.run(backfill_usage(storage, user_ids=["u1"])) == 1
        assert asyncio.run(backfill_usage(storage, user_ids=["u1"])) == 0, (
            "第二次回填应全部命中 seen，不再计量"
        )
        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        val = json.loads(next(iter(raw.values())))
        assert val["in"] == 10 and val["calls"] == 1

    def test_backfill_and_realtime_do_not_double_count(self, make_storage):
        """钩子已计量的消息，回填不得再计（同一 seen Set 串联两层）。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "a1", "m1")

        asyncio.run(
            storage.upsert_message(
                "u1", "s1", FakeMsg(id="m1", usage=FakeUsage(10, 10))
            )
        )
        assert asyncio.run(backfill_usage(storage, user_ids=["u1"])) == 0
        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        val = json.loads(next(iter(raw.values())))
        assert val["in"] == 10 and val["calls"] == 1

    def test_backfill_multiple_users_and_sessions(self, make_storage):
        """多用户多会话全扫，各自键各自聚合。"""
        import asyncio

        storage = make_storage()
        _seed_session(storage, "alice", "sa", "a1", "glm-4.7")
        _seed_session(storage, "alice", "sb", "a2", "doubao")
        _seed_session(storage, "bob", "sc", "a1", "glm-4.7")
        storage.buckets[("alice", "sa")] = [
            FakeMsg(id="m1", usage=FakeUsage(1, 1), created_at=_iso_days_ago(0))
        ]
        storage.buckets[("alice", "sb")] = [
            FakeMsg(id="m2", usage=FakeUsage(2, 2), created_at=_iso_days_ago(0))
        ]
        storage.buckets[("bob", "sc")] = [
            FakeMsg(id="m3", usage=FakeUsage(4, 4), created_at=_iso_days_ago(0))
        ]

        metered = asyncio.run(backfill_usage(storage))
        assert metered == 3

        alice = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="alice")))
        bob = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="bob")))
        assert len(alice) == 2, "alice 两个 (agent,模型) 组合两条 field"
        assert len(bob) == 1

    def test_backfill_bad_date_falls_back_to_today(self, make_storage):
        """消息无时间戳 → 日期归今天（不炸、不丢）。"""
        import asyncio

        storage = make_storage()
        _seed_session(storage, "u1", "s1", "a1", "m1")
        storage.buckets[("u1", "s1")] = [
            FakeMsg(id="m1", usage=FakeUsage(1, 1), created_at="", finished_at=None)
        ]
        asyncio.run(backfill_usage(storage, user_ids=["u1"]))
        raw = asyncio.run(storage._client.hgetall(_USAGE_KEY.format(user="u1")))
        date = next(iter(raw)).split("|", 1)[0]
        assert date == datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 查询 API

class TestUsageSummaryApi:
    """GET /usage/summary：聚合、排序、days 过滤、身份隔离、参数校验。"""

    def _client_with_data(self, make_storage, api_app, user: str = "u1"):
        """预置跨日期/跨 agent/跨模型的用量数据并装载 app。"""
        import asyncio

        storage = make_storage()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = {
            f"{today}|leader-1|glm-4.7": {"in": 100, "out": 50, "cache": 10, "calls": 3, "agent_name": "大A"},
            f"{_iso_days_ago(3)[:10]}|leader-1|glm-4.7": {"in": 30, "out": 20, "cache": 0, "calls": 1, "agent_name": "大A"},
            f"{_iso_days_ago(3)[:10]}|member-7|doubao-pro": {"in": 5, "out": 5, "cache": 0, "calls": 1, "agent_name": "政策研究员"},
            f"{_iso_days_ago(40)[:10]}|leader-1|glm-4.7": {"in": 999, "out": 999, "cache": 0, "calls": 9, "agent_name": "大A"},
        }
        for f, v in data.items():
            asyncio.run(
                storage._client.hset(_USAGE_KEY.format(user=user), f, json.dumps(v))
            )
        api_app.state.storage = storage
        return TestClient(api_app), today

    def test_summary_full_aggregation(self, make_storage, api_app):
        """30 天窗口：40 天前的数据被排除，其余按四维度正确聚合。"""
        client, today = self._client_with_data(make_storage, api_app)
        body = _summary(client, "u1", days=30)

        assert body["days"] == 30
        # 总计 = 今天 + 3 天前（40 天前被滤掉）
        assert body["totals"] == {"in": 135, "out": 75, "cache": 10, "calls": 5}

        # 按日期：今天一条 + 3 天前一条（合并了两个 agent 的量）
        by_date = {d["date"]: d for d in body["by_date"]}
        assert by_date[today] == {"date": today, "in": 100, "out": 50, "calls": 3}
        d3 = _iso_days_ago(3)[:10]
        assert by_date[d3] == {"date": d3, "in": 35, "out": 25, "calls": 2}

        # 按 agent：大A 在前（135 > 10），成员在后，名字带出
        agents = body["by_agent"]
        assert [a["name"] for a in agents] == ["大A", "政策研究员"]
        assert agents[0]["agent_id"] == "leader-1"
        assert agents[0]["in"] == 130 and agents[0]["calls"] == 4
        assert agents[1]["in"] == 5 and agents[1]["calls"] == 1

        # 按模型：glm-4.7 在前，doubao-pro 在后
        models = body["by_model"]
        assert [m["model"] for m in models] == ["glm-4.7", "doubao-pro"]
        assert models[0]["in"] == 130 and models[0]["calls"] == 4

    def test_summary_narrow_window_filters_old_days(self, make_storage, api_app):
        """7 天窗口仍含 3 天前；1 天窗口只剩今天。"""
        client, today = self._client_with_data(make_storage, api_app)

        body7 = _summary(client, "u1", days=7)
        assert {d["date"] for d in body7["by_date"]} == {today, _iso_days_ago(3)[:10]}

        body1 = _summary(client, "u1", days=1)
        assert {d["date"] for d in body1["by_date"]} == {today}
        assert body1["totals"]["in"] == 100

    def test_summary_identity_isolation(self, make_storage, api_app):
        """换 X-User-ID（模拟另一账号）→ 拿不到他人数据。"""
        client, _ = self._client_with_data(make_storage, api_app, user="alice")
        body = _summary(client, "bob", days=30)
        assert body["totals"] == {"in": 0, "out": 0, "cache": 0, "calls": 0}
        assert body["by_agent"] == [] and body["by_model"] == []

    def test_summary_empty_account(self, make_storage, api_app):
        """无任何记录的账号 → 全零 + 空列表（前端渲染空状态）。"""
        storage = make_storage()
        api_app.state.storage = storage
        client = TestClient(api_app)
        body = _summary(client, "nobody", days=30)
        assert body["totals"]["calls"] == 0
        assert body["by_date"] == []

    def test_summary_days_validation(self, make_storage, api_app):
        """days 边界：0 与 366 拒绝（422），1 与 365 放行。"""
        storage = make_storage()
        api_app.state.storage = storage
        client = TestClient(api_app)
        assert client.get("/usage/summary", params={"days": 0}).status_code == 422
        assert client.get("/usage/summary", params={"days": 366}).status_code == 422
        assert client.get("/usage/summary", params={"days": 1}).status_code == 200
        assert client.get("/usage/summary", params={"days": 365}).status_code == 200

    def test_summary_default_days_30(self, make_storage, api_app):
        """不传 days → 默认 30。"""
        storage = make_storage()
        api_app.state.storage = storage
        client = TestClient(api_app)
        res = client.get("/usage/summary", headers={"X-User-ID": "u"})
        assert res.status_code == 200
        assert res.json()["days"] == 30

    def test_summary_by_date_sorted_desc(self, make_storage, api_app):
        """by_date 必须倒序（最新在前），前端趋势图反转后贴底正序。"""
        client, today = self._client_with_data(make_storage, api_app)
        body = _summary(client, "u1", days=30)
        dates = [d["date"] for d in body["by_date"]]
        assert dates == sorted(dates, reverse=True)
        assert dates[0] == today


# ---------------------------------------------------------------- 端到端串联

class TestMeteringPipeline:
    """钩子 → 回填 → API 三层串联（模拟完整生命周期）。"""

    def test_realtime_then_query(self, make_storage, api_app):
        """新消息经钩子计量后，API 立即可查（无需重启/回填）。"""
        import asyncio

        storage = make_storage()
        patch_usage_metering(storage)
        _seed_session(storage, "u1", "s1", "leader-1", "glm-4.7")

        asyncio.run(
            storage.upsert_message(
                "u1", "s1",
                FakeMsg(id="m1", name="高考志愿专家", usage=FakeUsage(500, 250, 100)),
            )
        )

        api_app.state.storage = storage
        client = TestClient(api_app)
        body = _summary(client, "u1")
        assert body["totals"] == {"in": 500, "out": 250, "cache": 100, "calls": 1}
        assert body["by_agent"][0]["name"] == "高考志愿专家"
        assert body["by_model"][0]["model"] == "glm-4.7"


# ---------------------------------------------------------------- 产品维度（v2）

class TestProductAggregation:
    """products 维度：平台产品 = 大A及team（整体） / 独立小A（2026-09-08）。

    场景钉死用户核心表格：
        类型        名字       模型         输入   输出
        大A及团队   高考主理人  doubao-pro  (大A+成员合计)
        独立小A     政策研究员  glm-4.7     (自身)
    - leader 与其 member 的消耗必须并入同一个 team 产品
    - team 产品含 by_model 拆分与 members 成本构成
    - 无团队的 member / 独立 agent 单独成产品
    """

    @pytest.fixture
    def product_env(self, tmp_path, monkeypatch):
        """团队结构与类型文件指向 tmp（隔离生产 leader_teams.json）。"""
        teams = tmp_path / "teams.json"
        teams.write_text(
            json.dumps({"leader-1": ["member-1", "member-2"]}),
            encoding="utf-8",
        )
        types = tmp_path / "types.json"
        types.write_text(
            json.dumps({"leader-1": "leader", "member-1": "member", "solo-9": "member"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENTFORGE_LEADER_TEAMS_FILE", str(teams))
        monkeypatch.setenv("AGENTFORGE_AGENT_TYPES_FILE", str(types))
        return tmp_path

    def _seed(self, make_storage, api_app, user: str = "u1"):
        """预置：leader-1(team) + member-1/team + member-2/team + solo-9(独立)。"""
        import asyncio

        storage = make_storage()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = {
            f"{today}|leader-1|doubao-pro": {"in": 1000, "out": 400, "cache": 0, "calls": 4, "agent_name": "高考主理人"},
            f"{today}|member-1|doubao-pro": {"in": 300, "out": 150, "cache": 0, "calls": 2, "agent_name": "高考志愿兵"},
            f"{today}|member-2|glm-4.7": {"in": 200, "out": 100, "cache": 50, "calls": 1, "agent_name": "政策研究员"},
            f"{today}|solo-9|glm-4.7": {"in": 700, "out": 350, "cache": 0, "calls": 3, "agent_name": "独立专家"},
        }
        for f, v in data.items():
            asyncio.run(
                storage._client.hset(_USAGE_KEY.format(user=user), f, json.dumps(v))
            )
        api_app.state.storage = storage
        return TestClient(api_app)

    def test_team_members_merge_into_one_product(self, make_storage, api_app, product_env):
        """leader + member-1 + member-2 并入同一 team 产品，总量 = 三者之和。"""
        client = self._seed(make_storage, api_app)
        body = _summary(client, "u1")

        products = body["products"]
        team = next(p for p in products if p["type"] == "team")
        assert team["product_id"] == "leader-1"
        assert team["name"] == "高考主理人"
        assert team["agent_type"] == "leader"
        # team 总量 = 大A 1000/400 + 志愿兵 300/150 + 政策研究员 200/100
        assert team["in"] == 1500 and team["out"] == 650
        assert team["calls"] == 7 and team["cache"] == 50

    def test_team_product_contains_model_breakdown(self, make_storage, api_app, product_env):
        """team 行按模型拆分：doubao-pro（大A+志愿兵）与 glm-4.7（研究员）。"""
        client = self._seed(make_storage, api_app)
        team = next(p for p in _summary(client, "u1")["products"] if p["type"] == "team")

        by_model = {m["model"]: m for m in team["by_model"]}
        assert by_model["doubao-pro"]["in"] == 1300  # 1000 + 300
        assert by_model["glm-4.7"]["in"] == 200
        assert by_model["doubao-pro"]["calls"] == 6

    def test_team_product_contains_member_breakdown(self, make_storage, api_app, product_env):
        """team 成本构成：大A + 两个成员，各自名字与消耗可见。"""
        client = self._seed(make_storage, api_app)
        team = next(p for p in _summary(client, "u1")["products"] if p["type"] == "team")

        members = {m["agent_id"]: m for m in team["members"]}
        assert set(members) == {"leader-1", "member-1", "member-2"}
        assert members["leader-1"]["name"] == "高考主理人"
        assert members["member-1"]["in"] == 300
        # 成员行结构固定：无 cache 字段干扰
        assert set(members["member-2"]) == {"agent_id", "name", "in", "out", "calls"}

    def test_standalone_agent_is_separate_product(self, make_storage, api_app, product_env):
        """无团队的 solo-9 单独成产品（独立小A），不并入任何 team。"""
        client = self._seed(make_storage, api_app)
        products = {p["product_id"]: p for p in _summary(client, "u1")["products"]}

        solo = products["solo-9"]
        assert solo["type"] == "agent"
        assert solo["name"] == "独立专家"
        assert solo["in"] == 700 and solo["calls"] == 3
        assert "members" not in solo  # 独立产品无成员构成
        # 产品总数 = 1 个 team + 1 个独立
        assert len(products) == 2

    def test_products_sorted_by_consumption_desc(self, make_storage, api_app, product_env):
        """产品按总消耗降序：team(2150) 在 solo(1050) 前。"""
        client = self._seed(make_storage, api_app)
        products = _summary(client, "u1")["products"]
        totals = [p["in"] + p["out"] for p in products]
        assert totals == sorted(totals, reverse=True)
        assert products[0]["type"] == "team"

    def test_product_window_filtering(self, make_storage, api_app, product_env, monkeypatch):
        """窗口外的产品数据同样被 days 过滤（与 by_agent 一致）。"""
        import asyncio

        storage = make_storage()
        api_app.state.storage = storage
        old = _iso_days_ago(40)[:10]
        asyncio.run(
            storage._client.hset(
                _USAGE_KEY.format(user="u1"),
                f"{old}|member-1|doubao-pro",
                json.dumps({"in": 999, "out": 999, "cache": 0, "calls": 9, "agent_name": "高考志愿兵"}),
            )
        )
        body = _summary(TestClient(api_app), "u1", days=30)
        # member-1 只有 40 天前的消耗 → 不产生任何产品行
        assert body["products"] == []

    def test_leader_without_members_is_standalone(self, make_storage, api_app, product_env, monkeypatch):
        """未组队的 leader（teams 无记录）按独立产品处理。"""
        import asyncio

        # 清空团队结构：leader-9 没有成员
        (product_env / "teams.json").write_text("{}", encoding="utf-8")
        storage = make_storage()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        asyncio.run(
            storage._client.hset(
                _USAGE_KEY.format(user="u1"),
                f"{today}|leader-9|glm-4.7",
                json.dumps({"in": 10, "out": 5, "cache": 0, "calls": 1, "agent_name": "光杆大A"}),
            )
        )
        api_app.state.storage = storage
        products = _summary(TestClient(api_app), "u1")["products"]
        assert len(products) == 1
        assert products[0]["type"] == "agent"
        assert products[0]["name"] == "光杆大A"
