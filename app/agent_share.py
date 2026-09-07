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

from fastapi import APIRouter, HTTPException, Request
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
    if rec is None:
        return {"ok": True}  # 幂等：本就未发布
    if rec.get("owner") != user_id:
        raise HTTPException(status_code=403, detail="只有发布者可以取消发布")
    await _write_share(
        client, user_id, agent_id,
        rec.get("agent_name") or "", "private", [],
    )
    return {"ok": True}
