"""Token 用量计量与统计（2026-09-07 消费计量 v1；v2 产品维度）。

SaaS 前置能力：用户要知道"我这个账号的消耗情况——模型、大A/小A、
输入、输出"。

数据源：官方 ``Msg.usage``（input/output/cache tokens），消息经
``RedisStorage.upsert_message`` 落库（流式回复会多次 upsert 同 id：
首写 rpush usage 为空，后续 lset 更新出终值）。

计量管道（两层）：
1. **实时钩子**：patch ``upsert_message``——usage 有值且 message_id
   未计量过（``agentforge:usage:seen`` Set 去重，防流式 lset 重复
   累计）→ 按 ``(日期, agent_id, 模型)`` HINCRBY 聚合到
   ``agentforge:usage:{user}``（Hash，field=``{date}|{agent_id}|{model}``）
2. **存量回填**：``backfill_usage()`` 启动时扫全部会话消息（幂等：
   同靠 seen Set）——历史数据也进统计

归因：
- **模型**：会话当前 ``chat_model_config``（消息产生时点的配置；
  官方 Msg 不含模型名，历史消息以会话当前模型近似）
- **大A/小A**：session.agent_id + Msg.name（assistant 消息自带
  agent 显示名，主理人与成员各自落库自己的会话）

产品维度（v2，2026-09-08）：平台产出的产品只有两种形态——
**大A及team**（大A本体 + 全体成员小A 的整体消耗）与**独立小A**
（自身消耗）。``products`` 聚合按 ``LeaderTeamStore`` 的当前团队
结构归属：member 的消耗并入其所属大A 的 team 产品，方便直接看
"调用一次任务的成本"。

查询：``GET /usage/summary?days=N``——总计 + 按日期 + 按 agent +
按模型 + 按产品（含模型拆分与 team 成员构成）。存储键按 user_id
隔离（复用认证注入，天然多租户）。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from app.agent_type import AgentTypeStore, LEADER
from app.leader_team import LeaderTeamStore

_logger = logging.getLogger("agentforge.usage")

usage_router = APIRouter(tags=["agentforge"])

_USAGE_KEY = "agentforge:usage:{user}"
_SEEN_KEY = "agentforge:usage:seen"


def _field(date: str, agent_id: str, model: str) -> str:
    return f"{date}|{agent_id}|{model}"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def patch_usage_metering(storage: Any) -> None:
    """挂实时计量钩子。storage 为官方 RedisStorage。

    存量数据由 ``backfill_usage`` 处理（agent_service_app 启动钩子）。
    """
    original = type(storage).upsert_message

    # session_id → (agent_id, model) 内存缓存（会话数小，miss 全扫重建）
    session_cache: dict[str, tuple[str, str]] = {}

    async def metered_upsert(self: Any, user_id: str, session_id: str, msg: Any) -> None:
        await original(self, user_id, session_id, msg)
        try:
            usage = getattr(msg, "usage", None)
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cache_tok = getattr(usage, "cache_input_tokens", 0) or 0
            if not (in_tok or out_tok or cache_tok):
                return  # 流式首写/用户消息：不计
            msg_id = str(getattr(msg, "id", ""))
            if not msg_id:
                return
            # 去重：同一条消息的流式更新只计一次
            if await self._client.sismember(_SEEN_KEY, msg_id):
                return
            await self._client.sadd(_SEEN_KEY, msg_id)

            agent_id, model = await _resolve_session(self, user_id, session_id)
            agent_name = str(getattr(msg, "name", "") or agent_id[:8])
            field = _field(_today(), agent_id, model)
            key = _USAGE_KEY.format(user=user_id)
            raw = await self._client.hget(key, field)
            val = json.loads(raw) if raw else {
                "in": 0, "out": 0, "cache": 0, "calls": 0, "agent_name": agent_name,
            }
            val["in"] += in_tok
            val["out"] += out_tok
            val["cache"] += cache_tok
            val["calls"] += 1
            val["agent_name"] = agent_name
            await self._client.hset(key, field, json.dumps(val, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — 计量失败不影响消息落库
            _logger.exception("usage metering failed (session=%s)", session_id)

    async def _resolve_session(self: Any, user_id: str, session_id: str) -> tuple[str, str]:
        """session_id → (agent_id, 当前模型名)。直读 session 键。"""
        if session_id in session_cache:
            return session_cache[session_id]
        agent_id, model = "unknown", "unknown"
        try:
            raw = await self._client.get(
                f"agentscope:user:{user_id}:session:{session_id}"
            )
            if raw:
                rec = json.loads(raw)
                agent_id = rec.get("agent_id") or "unknown"
                mcfg = (rec.get("config") or {}).get("chat_model_config") or {}
                model = mcfg.get("model") or "unknown"
        except Exception:  # noqa: BLE001
            _logger.exception("usage: 解析会话归属失败 session=%s", session_id)
        session_cache[session_id] = (agent_id, model)
        return agent_id, model

    type(storage).upsert_message = metered_upsert  # type: ignore[method-assign]
    _logger.info("已挂 usage 计量钩子（upsert_message）")


async def _scan_user_sessions(storage: Any) -> dict[str, list[str]]:
    """扫会话**记录键** → ``{user_id: [session_id, ...]}``。

    必须扫记录键而非 ``sessions`` 索引键：官方 ``upsert_session``
    每次都写记录键（``agentscope:user:{u}:session:{sid}``），而
    ``agent:{aid}:sessions`` 索引只在新建时写——只认索引会漏掉
    部分用户（tests/test_usage_metering.py 回归锁定）。
    排除 ``:messages`` 列表键与段数不符的键。
    """
    result: dict[str, list[str]] = {}
    cursor = 0
    while True:
        cursor, keys = await storage._client.scan(
            cursor=cursor, match="agentscope:user:*:session:*", count=100
        )
        for k in keys:
            if k.endswith(":messages"):
                continue
            parts = k.split(":")
            # agentscope:user:{u}:session:{sid} → 恰好 5 段
            if len(parts) == 5:
                result.setdefault(parts[2], []).append(parts[4])
        if cursor == 0:
            break
    return result


async def backfill_usage(storage: Any, user_ids: list[str] | None = None) -> int:
    """存量回填：扫全部会话消息，usage 进统计（幂等，靠 seen Set）。

    Returns:
        本次新计量的消息条数。
    """
    metered = 0
    # 显式 user_ids：只回填指定用户；缺省全量（扫记录键发现用户）
    all_sessions = await _scan_user_sessions(storage)
    if user_ids is None:
        user_sessions = all_sessions
    else:
        user_sessions = {u: all_sessions.get(u, []) for u in user_ids}
    for user_id, sids in user_sessions.items():
        for sid in sids:
            raw = await storage._client.get(
                f"agentscope:user:{user_id}:session:{sid}"
            )
            if not raw:
                continue
            rec = json.loads(raw)
            agent_id = rec.get("agent_id") or "unknown"
            mcfg = (rec.get("config") or {}).get("chat_model_config") or {}
            model = mcfg.get("model") or "unknown"
            msgs, _more = await storage.list_messages(user_id, sid, limit=10_000)
            for m in msgs:
                usage = getattr(m, "usage", None)
                in_tok = getattr(usage, "input_tokens", 0) or 0
                out_tok = getattr(usage, "output_tokens", 0) or 0
                cache_tok = getattr(usage, "cache_input_tokens", 0) or 0
                if not (in_tok or out_tok or cache_tok):
                    continue
                msg_id = str(getattr(m, "id", ""))
                if not msg_id or await storage._client.sismember(_SEEN_KEY, msg_id):
                    continue
                await storage._client.sadd(_SEEN_KEY, msg_id)
                date = (getattr(m, "finished_at", None) or getattr(m, "created_at", "") or "")[:10]
                date = date if len(date) == 10 else _today()
                field = _field(date, agent_id, model)
                key = _USAGE_KEY.format(user=user_id)
                raw = await storage._client.hget(key, field)
                val = json.loads(raw) if raw else {
                    "in": 0, "out": 0, "cache": 0, "calls": 0,
                    "agent_name": str(getattr(m, "name", "") or agent_id[:8]),
                }
                val["in"] += in_tok
                val["out"] += out_tok
                val["cache"] += cache_tok
                val["calls"] += 1
                await storage._client.hset(key, field, json.dumps(val, ensure_ascii=False))
                metered += 1
    if metered:
        _logger.info("usage 回填完成：新计量 %d 条", metered)
    return metered


@usage_router.get("/usage/summary")
async def usage_summary(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """账号用量汇总：总计 + 按日期 + 按大A/小A + 按模型 + 按产品。

    产品维度（v2）：平台产品 = 大A及team（leader + 全体成员的整体
    消耗）或 独立小A。member 消耗并入所属大A 的 team 产品——直观看
    "调用一次任务的成本"（team 行含模型拆分 + 成员构成明细）。

    复用认证中间件注入的 X-User-ID（多租户隔离随主链路）。
    """
    storage = request.app.state.storage
    user_id = request.headers.get("X-User-ID", "")
    raw = await storage._client.hgetall(_USAGE_KEY.format(user=user_id)) or {}

    # 起始日期（UTC，粗粒度天级）
    from datetime import timedelta

    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    # 产品归属映射（当前团队结构）：member → leader；leader 自成 team
    teams = LeaderTeamStore().load()
    member_to_leader: dict[str, str] = {}
    for leader_id, member_ids in teams.items():
        for m in member_ids:
            # 一个 member 被多个团队引用时归第一个（v1 简化）
            member_to_leader.setdefault(m, leader_id)

    agent_types = AgentTypeStore().load()

    def _product_of(agent_id: str) -> tuple[str, str]:
        """agent_id → (产品类型, 产品主体 id)。team=大A及团队，agent=独立。"""
        if agent_id in teams and teams[agent_id]:
            return "team", agent_id
        leader = member_to_leader.get(agent_id)
        if leader:
            return "team", leader
        return "agent", agent_id

    totals = {"in": 0, "out": 0, "cache": 0, "calls": 0}
    by_date: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    # 产品聚合：product_id → 总量；嵌套 by_model / 成员构成（team 专有）
    products: dict[str, dict] = {}

    def _bump(bucket: dict, val: dict) -> None:
        bucket["in"] += val["in"]
        bucket["out"] += val["out"]
        bucket["calls"] += val["calls"]

    for field, val_json in raw.items():
        date, agent_id, model = field.split("|", 2)
        val = json.loads(val_json)
        if date < start:
            continue
        totals["in"] += val["in"]
        totals["out"] += val["out"]
        totals["cache"] += val["cache"]
        totals["calls"] += val["calls"]

        d = by_date.setdefault(date, {"in": 0, "out": 0, "calls": 0})
        d["in"] += val["in"]
        d["out"] += val["out"]
        d["calls"] += val["calls"]

        a = by_agent.setdefault(
            agent_id,
            {"agent_id": agent_id, "name": val.get("agent_name", agent_id[:8]),
             "in": 0, "out": 0, "calls": 0},
        )
        _bump(a, val)

        m = by_model.setdefault(model, {"model": model, "in": 0, "out": 0, "calls": 0})
        _bump(m, val)

        # 产品维度
        ptype, pid = _product_of(agent_id)
        p = products.setdefault(
            pid,
            {
                "type": ptype,
                "product_id": pid,
                "name": "",
                "agent_type": agent_types.get(pid, "member"),
                "in": 0, "out": 0, "cache": 0, "calls": 0,
                "by_model": {},
                "members": {},
            },
        )
        _bump(p, val)
        p["cache"] += val["cache"]
        pm = p["by_model"].setdefault(model, {"model": model, "in": 0, "out": 0, "calls": 0})
        _bump(pm, val)
        mem = p["members"].setdefault(
            agent_id,
            {"agent_id": agent_id, "name": val.get("agent_name", agent_id[:8]),
             "in": 0, "out": 0, "calls": 0},
        )
        _bump(mem, val)

    # 产品名：team 用大A 名字（优先用量数据，其次 agent 存储）
    for pid, p in products.items():
        if p["type"] == "team":
            leader_val = p["members"].get(pid)
            p["name"] = leader_val["name"] if leader_val else f"团队({pid[:8]})"
        else:
            p["name"] = next(iter(p["members"].values()))["name"]

    products_out = []
    for p in products.values():
        entry = {
            "type": p["type"],
            "product_id": p["product_id"],
            "name": p["name"],
            "agent_type": p["agent_type"],
            "in": p["in"], "out": p["out"], "cache": p["cache"], "calls": p["calls"],
            "by_model": sorted(
                p["by_model"].values(), key=lambda x: -(x["in"] + x["out"]),
            ),
        }
        if p["type"] == "team":
            entry["members"] = sorted(
                p["members"].values(), key=lambda x: -(x["in"] + x["out"]),
            )
        products_out.append(entry)

    return {
        "days": days,
        "totals": totals,
        "by_date": [
            {"date": k, **v} for k, v in sorted(by_date.items(), reverse=True)
        ],
        "by_agent": sorted(by_agent.values(), key=lambda x: -(x["in"] + x["out"])),
        "by_model": sorted(by_model.values(), key=lambda x: -(x["in"] + x["out"])),
        "products": sorted(products_out, key=lambda x: -(x["in"] + x["out"])),
    }
