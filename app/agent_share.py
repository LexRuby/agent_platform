"""智能体共享（账号 → 智能体可见性管理）——2026-09-07 共享 v1。

用户需求："我能选择我发布之后，这些大A/小A 是什么账号能看到的？"
——账号隔离已天然存在（所有数据按 user_id 键控），缺的是**可见性
控制**：把指定智能体发布给指定账号（或全部账号）。

官方框架已预留完整扩展点（agentscope.app.access）：
- ``ResourceAccessPolicyBase.list_accessible(viewer, kind, storage)``
  → 返回跨 owner 的 ``ResourceRef`` 列表
- ``ResourceAccessService`` 负责合并（自己的 + 被共享的）与只读
  保护：``GET /agent/`` 列表自动带 ``editable=false`` 徽标；
  ``PATCH/DELETE /agent/{id}`` 走 ``resolve_for_edit`` → 403；
  会话创建与聊天运行走 ``resolve_agent`` → 未授权 404
- 前端 AgentSelect 已内置"共享给我"分组与只读禁用

本模块补上最后一块：**Redis 授权存储 + 管理端点**。

数据结构（Redis，全在 agentforge:share:* 命名空间）：
- ``agentforge:share:agent:{agent_id}`` → JSON
  ``{owner, mode: "users"|"public", users: [...], agent_name, shared_at}``
  （不存在的键 = 私有，未发布）
- ``agentforge:share:to:{user}`` → Set：显式授权给该用户的 agent_id
- ``agentforge:share:public`` → Set：公开的 agent_id
- ``agentforge:share:owner:{owner}`` → Set：该 owner 发布过的 agent_id

端点（agent_share_router）：
- ``GET  /agent-share/mine``   我发布的智能体（管理页数据源）
- ``PUT  /agent-share/{agent_id}``  发布/更新可见性（仅 owner）
- ``DELETE /agent-share/{agent_id}``  取消发布（仅 owner）

权限语义：共享一律只读（READ）。跨 owner 编辑（EDIT 权限）留待
后续按需开放——SaaS 消费场景（客户用大小A）只读即可，且防止
消费者误改生产版本。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)
from agentscope.app.storage import StorageBase

_logger = logging.getLogger("agentforge.agent_share")

agent_share_router = APIRouter(tags=["agent-share"])

# Redis 键模板
_AGENT_KEY = "agentforge:share:agent:{agent_id}"
_TO_KEY = "agentforge:share:to:{user}"
_PUBLIC_KEY = "agentforge:share:public"
_OWNER_KEY = "agentforge:share:owner:{owner}"
# 发布物元数据（版本化发布，2026-09-08）
_PUBMETA_KEY = "agentforge:share:pubmeta:{published_id}"
_PUBS_KEY = "agentforge:share:pubs:{owner}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RedisAgentSharePolicy(ResourceAccessPolicyBase):
    """跨 owner 智能体访问策略：按 Redis 授权记录放行（只读）。

    注入 ``create_app(resource_access_policy=...)`` 后，官方的列表/
    详情/编辑/会话/聊天全链路自动生效。
    """

    def __init__(self, storage: StorageBase) -> None:
        self._storage = storage

    async def list_accessible(
        self,
        viewer_id: str,
        kind: ResourceKind,
        storage: StorageBase,
    ) -> list[ResourceRef]:
        if kind is not ResourceKind.AGENT:
            return []  # v1 只共享智能体（凭据/知识库不跨账号）
        client = self._client()
        # 显式授权 + 公开，去重
        granted = await client.smembers(_TO_KEY.format(user=viewer_id))
        public = await client.smembers(_PUBLIC_KEY)
        refs: list[ResourceRef] = []
        for agent_id in granted | public:
            raw = await client.get(_AGENT_KEY.format(agent_id=agent_id))
            if not raw:
                continue  # 授权记录已清（残留 Set 成员），跳过
            try:
                rec = json.loads(raw)
            except (TypeError, ValueError):
                continue
            owner = rec.get("owner") or ""
            if not owner or owner == viewer_id:
                continue  # 自己的智能体走官方自有路径，不重复出现
            refs.append(
                ResourceRef(
                    kind=ResourceKind.AGENT,
                    owner_id=owner,
                    resource_id=agent_id,
                    permission=ResourcePermission.READ,
                ),
            )
        return refs

    def _client(self) -> Any:
        """官方 RedisStorage 的连接（策略与路由共用同一 storage）。"""
        return self._storage._client  # noqa: SLF001 — 官方未暴露只读接口


# ── 请求/响应模型 ──────────────────────────────────────────────────────

class ShareVisibilityRequest(BaseModel):
    """发布设置：mode=private 时等价于取消发布。"""

    mode: str = Field(description="可见范围：private | users | public")
    users: list[str] = Field(
        default_factory=list,
        description="mode=users 时的授权账号列表（用户名）",
    )


class PublishRequest(BaseModel):
    """版本化发布：从源智能体的指定版本快照复制出对外产品并共享。

    用户场景：高考志愿兵 v1.0 基础 → v2.0 文科优化 → v3.0 理科优化；
    分别发布 v2.0（对外名"文科志愿专家"）与 v3.0（"理科志愿专家"），
    两个产品独立存在，源智能体继续迭代互不影响。
    """

    agent_id: str = Field(description="源智能体 id")
    version: int = Field(description="要发布的版本号（快照）")
    display_name: str = Field(description="对外名称（重命名）")
    mode: str = Field(description="可见范围：users | public（发布必共享）")
    users: list[str] = Field(
        default_factory=list,
        description="mode=users 时的授权账号列表（用户名）",
    )
    team_mode: str = Field(
        default="blueprint",
        description=(
            "团队形态：blueprint=固定团队（快照图纸注入，按定义重建成员）"
            " | auto=自动组建（不注入名单，保留组队能力按任务即兴组队）"
        ),
    )


class PublicationInfo(BaseModel):
    """发布物（对外产品）信息。"""

    agent_id: str = Field(description="发布物智能体 id（独立个体）")
    display_name: str
    source_agent_id: str
    source_agent_name: str = ""
    source_version: int
    mode: str = "users"
    users: list[str] = []
    published_at: str = ""
    team_mode: str = "blueprint"


class MyPublicationsResponse(BaseModel):
    publications: list[PublicationInfo]


class ShareInfo(BaseModel):
    """单个智能体的共享状态（管理页行数据）。"""

    agent_id: str
    agent_name: str
    mode: str  # private | users | public
    users: list[str] = []
    shared_at: str = ""


class MySharesResponse(BaseModel):
    shares: list[ShareInfo]


# ── 辅助 ──────────────────────────────────────────────────────────────

def _require_user(request: Request) -> str:
    user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return user_id


async def _load_share(client: Any, agent_id: str) -> dict | None:
    raw = await client.get(_AGENT_KEY.format(agent_id=agent_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def _write_share(
    client: Any,
    owner: str,
    agent_id: str,
    agent_name: str,
    mode: str,
    users: list[str],
) -> None:
    """写授权记录并维护索引 Set（先清旧成员再入新成员）。"""
    old = await _load_share(client, agent_id)
    # 清旧索引
    if old:
        for u in old.get("users") or []:
            await client.srem(_TO_KEY.format(user=u), agent_id)
        if old.get("mode") == "public":
            await client.srem(_PUBLIC_KEY, agent_id)
    # 记录
    if mode == "private":
        await client.delete(_AGENT_KEY.format(agent_id=agent_id))
        await client.srem(_OWNER_KEY.format(owner=owner), agent_id)
        return
    rec = {
        "owner": owner,
        "mode": mode,
        "users": users if mode == "users" else [],
        "agent_name": agent_name,
        "shared_at": (old or {}).get("shared_at") or _now_iso(),
    }
    await client.set(
        _AGENT_KEY.format(agent_id=agent_id),
        json.dumps(rec, ensure_ascii=False),
    )
    if mode == "users":
        for u in users:
            await client.sadd(_TO_KEY.format(user=u), agent_id)
    else:  # public
        await client.sadd(_PUBLIC_KEY, agent_id)
    await client.sadd(_OWNER_KEY.format(owner=owner), agent_id)


def _valid_username(name: str) -> bool:
    import re

    return bool(re.match(r"^[a-zA-Z0-9_-]{2,32}$", name))


# ── 端点 ──────────────────────────────────────────────────────────────

@agent_share_router.get(
    "/agent-share/mine",
    response_model=MySharesResponse,
    summary="我发布的智能体及可见性（管理页数据源）",
)
async def list_my_shares(request: Request) -> MySharesResponse:
    user_id = _require_user(request)
    client = request.app.state.storage._client
    agent_ids = await client.smembers(_OWNER_KEY.format(owner=user_id))
    shares: list[ShareInfo] = []
    for agent_id in agent_ids:
        rec = await _load_share(client, agent_id)
        if not rec or rec.get("owner") != user_id:
            continue  # 残留索引：记录已删/已换主
        shares.append(
            ShareInfo(
                agent_id=agent_id,
                agent_name=rec.get("agent_name") or agent_id[:8],
                mode=rec.get("mode") or "users",
                users=rec.get("users") or [],
                shared_at=rec.get("shared_at") or "",
            ),
        )
    shares.sort(key=lambda s: s.agent_name)
    return MySharesResponse(shares=shares)


@agent_share_router.put(
    "/agent-share/{agent_id}",
    response_model=ShareInfo,
    summary="发布/更新智能体可见性（仅 owner）",
)
async def set_share_visibility(
    agent_id: str,
    body: ShareVisibilityRequest,
    request: Request,
) -> ShareInfo:
    user_id = _require_user(request)
    storage = request.app.state.storage

    # 必须是自己的智能体（不允许替他人发布）
    agent = await storage.get_agent(user_id, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail="智能体不存在（或不在你的账号下）",
        )
    if agent.source == "team":
        raise HTTPException(
            status_code=400,
            detail="团队成员智能体随团队生命周期管理，不支持单独共享",
        )

    mode = body.mode
    if mode not in ("private", "users", "public"):
        raise HTTPException(status_code=422, detail="mode 必须是 private/users/public")
    users = sorted({u.strip() for u in body.users if u.strip()})
    if mode == "users" and not users:
        raise HTTPException(status_code=422, detail="指定账号模式需要至少一个账号")
    for u in users:
        if not _valid_username(u):
            raise HTTPException(
                status_code=422, detail=f"账号名不合法: {u}（2-32 位字母数字_-）",
            )

    await _write_share(
        storage._client, user_id, agent_id,
        agent.data.name, mode, users,
    )
    rec = await _load_share(storage._client, agent_id)
    return ShareInfo(
        agent_id=agent_id,
        agent_name=agent.data.name,
        mode=mode,
        users=users if mode == "users" else [],
        shared_at=(rec or {}).get("shared_at") or _now_iso(),
    )


@agent_share_router.delete(
    "/agent-share/{agent_id}",
    summary="取消发布（仅 owner）",
)
async def unpublish_share(agent_id: str, request: Request) -> dict:
    user_id = _require_user(request)
    client = request.app.state.storage._client
    rec = await _load_share(client, agent_id)
    if rec is not None and rec.get("owner") != user_id:
        raise HTTPException(status_code=403, detail="只有发布者可以取消发布")
    await _write_share(
        client, user_id, agent_id,
        (rec or {}).get("agent_name") or "", "private", [],
    )
    # 发布物：顺带清溯源元数据（下架后 pubs 列表不再出现）
    await client.delete(_PUBMETA_KEY.format(published_id=agent_id))
    await client.srem(_PUBS_KEY.format(owner=user_id), agent_id)
    return {"ok": True}


# ── 版本化发布（v2，2026-09-08）：发布 = 版本快照复制 + 共享 ─────────────

@agent_share_router.post(
    "/agent-share/publish",
    response_model=PublicationInfo,
    summary="版本化发布：从指定版本快照复制出对外产品并共享",
)
async def publish_version(body: PublishRequest, request: Request) -> PublicationInfo:
    from .agent_version import AgentVersionStore, duplicate_agent_core

    user_id = _require_user(request)
    mode = body.mode
    if mode not in ("users", "public"):
        raise HTTPException(
            status_code=422, detail="发布模式必须是 users/public（发布必须共享）",
        )
    users = sorted({u.strip() for u in body.users if u.strip()})
    if mode == "users" and not users:
        raise HTTPException(status_code=422, detail="指定账号模式需要至少一个账号")
    for u in users:
        if not _valid_username(u):
            raise HTTPException(
                status_code=422, detail=f"账号名不合法: {u}（2-32 位字母数字_-）",
            )
    display_name = body.display_name.strip()[:100]
    if not display_name:
        raise HTTPException(status_code=422, detail="对外名称不能为空")
    if body.team_mode not in ("blueprint", "auto"):
        raise HTTPException(
            status_code=422, detail="团队形态必须是 blueprint/auto",
        )

    # 版本必须存在（发布的是快照，不是实时配置）
    store = AgentVersionStore()
    if store.get_version(body.agent_id, body.version) is None:
        raise HTTPException(
            status_code=404, detail=f"版本 v{body.version} 不存在（先在版本中心发版）",
        )

    # 核心：从版本快照复制出对外产品（独立个体）
    dup = await duplicate_agent_core(
        body.agent_id, user_id, display_name, body.version,
        request.app.state.storage, team_mode=body.team_mode,
    )
    published_id = dup["agent_id"]

    client = request.app.state.storage._client
    # 可见性（复用 v1 共享机制）
    await _write_share(client, user_id, published_id, display_name, mode, users)
    # 发布物元数据（溯源：源 agent + 源版本）
    pubmeta = {
        "source_agent_id": body.agent_id,
        "source_version": body.version,
        "display_name": display_name,
        "published_at": _now_iso(),
        "team_mode": body.team_mode,
    }
    await client.set(
        _PUBMETA_KEY.format(published_id=published_id),
        json.dumps(pubmeta, ensure_ascii=False),
    )
    await client.sadd(_PUBS_KEY.format(owner=user_id), published_id)
    return PublicationInfo(
        agent_id=published_id,
        display_name=display_name,
        source_agent_id=body.agent_id,
        source_agent_name="",
        source_version=body.version,
        mode=mode,
        users=users if mode == "users" else [],
        published_at=pubmeta["published_at"],
        team_mode=body.team_mode,
    )


@agent_share_router.get(
    "/agent-share/pubs",
    response_model=MyPublicationsResponse,
    summary="我的发布物列表（对外产品 + 溯源信息）",
)
async def list_my_publications(request: Request) -> MyPublicationsResponse:
    user_id = _require_user(request)
    client = request.app.state.storage._client
    # 源智能体名映射（溯源展示）
    name_map: dict[str, str] = {}
    try:
        from agent_service_app import _official_app  # noqa: PLC0415

        agents = await _official_app.state.storage.list_agents(user_id)
        name_map = {a.id: a.data.name for a in agents}
    except Exception:  # noqa: BLE001 — 列表失败不阻断发布物查询
        name_map = {}

    pubs: list[PublicationInfo] = []
    for pid in await client.smembers(_PUBS_KEY.format(owner=user_id)):
        raw = await client.get(_PUBMETA_KEY.format(published_id=pid))
        if not raw:
            continue  # 残留索引
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            continue
        share = await _load_share(client, pid)
        pubs.append(
            PublicationInfo(
                agent_id=pid,
                display_name=meta.get("display_name") or pid[:8],
                source_agent_id=meta.get("source_agent_id") or "",
                source_agent_name=name_map.get(
                    meta.get("source_agent_id") or "", "",
                ),
                source_version=meta.get("source_version") or 0,
                mode=(share or {}).get("mode") or "users",
                users=(share or {}).get("users") or [],
                published_at=meta.get("published_at") or "",
                team_mode=meta.get("team_mode") or "blueprint",
            ),
        )
    pubs.sort(key=lambda p: p.published_at, reverse=True)
    return MyPublicationsResponse(publications=pubs)


# ── 发布物使用统计（发布者视角，2026-09-09）：培育闭环的数据回顾 ──────────


class PublicationUsage(BaseModel):
    """单个发布物的使用统计（跨用户聚合「大A及团队」整体消耗）。

    所有者回顾"产品被谁用了、跑了多少任务、token 烧在哪"的依据——
    迭代决策的输入（和 agent 对话 + 使用数据回顾）。
    """

    agent_id: str
    display_name: str
    source_version: int = 0
    published_at: str = ""
    team_mode: str = "blueprint"
    active_users: int = 0
    """时间窗内用过该产品的用户数"""
    totals: dict = Field(
        default_factory=lambda: {"in": 0, "out": 0, "cache": 0, "calls": 0},
    )
    by_date: list[dict] = []
    by_model: list[dict] = []
    members: list[dict] = []
    """按成员名跨用户聚合（同名成员合并——图纸保证名字一致，
    发布者关心的是"哪个专家角色烧了多少 token"）"""


@agent_share_router.get(
    "/agent-share/pubs/usage",
    summary="我的发布物使用统计（发布者视角：跨用户聚合大A及团队消耗）",
)
async def pubs_usage(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """发布者视角的使用数据回顾（培育闭环的反馈输入）。

    与 ``/usage/summary``（消费者视角，查自己消费多少）互补——这里
    跨全平台用户聚合**自己发布的**产品使用量：用户用产品组队时，
    主理人本体 + 在该用户账号里重建的全体团队成员消耗全部归入
    该发布产品（「大A及团队」口径 = 调用一次任务的完整成本）。

    隐私边界：只暴露聚合数据（活跃用户数/总量/趋势/角色构成），
    不暴露具体用户的会话内容。
    """
    user_id = _require_user(request)
    storage = request.app.state.storage
    client = storage._client
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    start = (
        datetime.now(timezone.utc) - timedelta(days=days - 1)
    ).strftime("%Y-%m-%d")

    # 我的发布物集合 + 元数据
    pub_ids = await client.smembers(_PUBS_KEY.format(owner=user_id))
    pub_meta: dict[str, dict] = {}
    for pid in pub_ids:
        raw = await client.get(_PUBMETA_KEY.format(published_id=pid))
        if not raw:
            continue
        try:
            pub_meta[pid] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    if not pub_meta:
        return {"days": days, "publications": []}

    # 聚合骨架
    agg: dict[str, dict] = {
        pid: {
            "meta": meta,
            "users": set(),
            "totals": {"in": 0, "out": 0, "cache": 0, "calls": 0},
            "by_date": {},
            "by_model": {},
            "members": {},
        }
        for pid, meta in pub_meta.items()
    }

    # 扫全平台 usage 键（每用户一个；跳过去重 Set）
    from .usage_metering import _SEEN_KEY, _USAGE_KEY, load_team_structure  # noqa: PLC0415

    async for key in client.scan_iter(match="agentforge:usage:*"):
        if key == _SEEN_KEY:
            continue
        uid = key[len("agentforge:usage:"):]
        raw_map = await client.hgetall(key)
        if not raw_map:
            continue
        # 该用户账号里，各发布物领导的团队成员（动态组队的成员归入产品）
        try:
            _, m2l = await load_team_structure(storage, uid)
        except Exception:  # noqa: BLE001 — 团队结构缺失按无成员处理
            m2l = {}
        for field, val_json in raw_map.items():
            try:
                date, agent_id, model = field.split("|", 2)
                val = json.loads(val_json)
            except (ValueError, TypeError):
                continue
            if date < start:
                continue
            # 归属：产品本体 或 产品在该用户账号里领导的团队成员
            if agent_id in agg:
                pid = agent_id
            else:
                leader = m2l.get(agent_id)
                if leader not in agg:
                    continue  # 别人的/自己的其他智能体，不混入
                pid = leader
            a = agg[pid]
            a["users"].add(uid)
            for k in ("in", "out", "cache", "calls"):
                a["totals"][k] += val.get(k, 0)
            d = a["by_date"].setdefault(
                date, {"date": date, "in": 0, "out": 0, "calls": 0},
            )
            d["in"] += val.get("in", 0)
            d["out"] += val.get("out", 0)
            d["calls"] += val.get("calls", 0)
            m = a["by_model"].setdefault(
                model, {"model": model, "in": 0, "out": 0, "calls": 0},
            )
            m["in"] += val.get("in", 0)
            m["out"] += val.get("out", 0)
            m["calls"] += val.get("calls", 0)
            # 成员构成按名聚合（跨用户同名合并，主理人本体名=产品名）
            name = val.get("agent_name") or agent_id[:8]
            mem = a["members"].setdefault(
                name, {"name": name, "in": 0, "out": 0, "calls": 0},
            )
            mem["in"] += val.get("in", 0)
            mem["out"] += val.get("out", 0)
            mem["calls"] += val.get("calls", 0)

    # 输出（按发布时间降序，与 pubs 列表一致）
    out = []
    for pid, a in agg.items():
        meta = a["meta"]
        out.append(PublicationUsage(
            agent_id=pid,
            display_name=meta.get("display_name") or pid[:8],
            source_version=meta.get("source_version") or 0,
            published_at=meta.get("published_at") or "",
            team_mode=meta.get("team_mode") or "blueprint",
            active_users=len(a["users"]),
            totals=a["totals"],
            by_date=sorted(
                a["by_date"].values(), key=lambda x: x["date"], reverse=True,
            ),
            by_model=sorted(
                a["by_model"].values(),
                key=lambda x: -(x["in"] + x["out"]),
            ),
            members=sorted(
                a["members"].values(),
                key=lambda x: -(x["in"] + x["out"]),
            ),
        ))
    out.sort(key=lambda p: p.published_at, reverse=True)
    return {"days": days, "publications": out}
