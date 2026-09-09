"""Agent 版本封板（freeze）：培育 → 封板 → 对外服务的版本管理。

用户模型：
- **冻结**：把 agent 当前配置（提示词/设置）快照成带版本号的封板。
  冻结期间 ``PATCH /agent/{id}`` 被拦截（403），自我迭代/升级停止；
- **解冻**：开放模式，恢复可编辑；
- **保存版本**：开放模式下迭代到满意时手动保存 → 产生新版本号；
- **恢复版本**：回到历史版本的配置。显式人工操作即授权，
  冻结中也可执行（经官方端点直写，不走拦截链）。

组件：
- :class:`AgentVersionStore`：``data/agent_versions.json`` 持久化
  ``{agent_id: {frozen, current_version, versions: [...]}}``
- :class:`AgentVersionMiddleware`：纯 ASGI 中间件——
  - ``PATCH /agent/{id}``：冻结中 → 403（中文说明）
  - ``GET /agent/``：每个 agent 注入 ``version`` 字段供前端回显
  - ``DELETE /agent/{id}``：清理 sidecar
- 路由（:data:`agent_version_router`）：
  - ``POST /agent/{id}/freeze`` / ``unfreeze`` / ``save-version``
  - ``GET  /agent/{id}/versions``（列表）/ ``versions/{v}``（详情）
  - ``POST /agent/{id}/versions/{v}/restore``

注意中间件包装顺序（生产见 agent_service_app.py）：
AgentVersionMiddleware 在最外层（Auth 之内），保证冻结的 PATCH
在 agent_type / leader_team 处理前就被拦下，不产生任何副作用。
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agentscope.app.access import ResourceKind

from .agent_type import _replay
from .team_archive import _call_official

_logger = logging.getLogger("agentforge.agent_version")

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILE = _BASE_DIR / "data" / "agent_versions.json"

# 官方 AgentData 的配置字段（快照/恢复的载荷；不含运行时元数据）
CONFIG_FIELDS = (
    "name", "system_prompt", "context_config", "react_config", "invite_config",
)


def _versions_file() -> Path:
    return Path(
        os.environ.get("AGENTFORGE_AGENT_VERSIONS_FILE", str(_DEFAULT_FILE)),
    )


def _new_record() -> dict:
    return {"frozen": False, "current_version": None, "versions": []}


class AgentVersionStore:
    """agent_id → 版本记录 的文件持久化。单 worker 部署下读写足够。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _versions_file()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 - 损坏文件降级为空
            _logger.warning("agent 版本文件损坏（按空处理）%s: %s", self.path, e)
            return {}

    def _save_all(self, mapping: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def record(self, agent_id: str) -> dict:
        """该 agent 的版本记录（无则返回空白记录，不落盘）。"""
        rec = self.load().get(agent_id)
        if not isinstance(rec, dict):
            return _new_record()
        return {
            "frozen": bool(rec.get("frozen")),
            "current_version": rec.get("current_version"),
            "versions": [v for v in rec.get("versions") or [] if isinstance(v, dict)],
        }

    def save(self, agent_id: str, rec: dict) -> None:
        mapping = self.load()
        if rec["versions"] or rec["frozen"]:
            mapping[agent_id] = rec
        else:  # 空记录不占文件
            mapping.pop(agent_id, None)
        self._save_all(mapping)

    def delete(self, agent_id: str) -> None:
        mapping = self.load()
        if agent_id in mapping:
            del mapping[agent_id]
            self._save_all(mapping)

    def is_frozen(self, agent_id: str) -> bool:
        return self.record(agent_id)["frozen"]

    def latest_version(self, agent_id: str) -> int | None:
        versions = self.record(agent_id)["versions"]
        return versions[-1]["version"] if versions else None

    def get_version(self, agent_id: str, version: int) -> dict | None:
        return next(
            (v for v in self.record(agent_id)["versions"] if v.get("version") == version),
            None,
        )

    def add_version(
        self, agent_id: str, data: dict, label: str = "", *, force: bool = False,
        team_blueprint: dict | None = None,
    ) -> dict:
        """追加版本快照。返回版本条目。

        - ``force=False``（freeze 用）：与最新版本内容一致时复用——
          冻结→解冻→再冻结不产生冗余版本号；
        - ``force=True``（save-version 用）：显式发版动作，**总是新增**
          ——用户点了「发布新版本」按钮，即使配置未变也要有反馈
          （历史 bug：静默复用导致界面上"点了没反应"）。
        - ``team_blueprint``（2026-09-09 方案 A）：主理人发版时把团队
          定义（团队名/宗旨 + 成员名/职责/完整提示词）打进快照。
          发布 = 从快照复制产品 → 产品提示词自动注入团队图纸，
          组队时按定义重建成员（培养资产随版本走，不再只发主理人
          壳子）。dedup 比较含图纸（团队变了 = 新版本）。
        """
        rec = self.record(agent_id)
        payload = {k: data[k] for k in CONFIG_FIELDS if k in data}
        if team_blueprint:
            payload["team_blueprint"] = team_blueprint
        if (
            not force
            and rec["versions"]
            and rec["versions"][-1].get("data") == payload
        ):
            return rec["versions"][-1]
        version = (rec["versions"][-1]["version"] + 1) if rec["versions"] else 1
        entry = {
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "label": (label or "").strip()[:200],
            "data": payload,
        }
        rec["versions"].append(entry)
        self.save(agent_id, rec)
        return entry


# ── 请求/响应模型 ──────────────────────────────────────────────────────

class VersionBrief(BaseModel):
    version: int
    created_at: str
    label: str = ""
    """团队图纸成员数（方案 A）：>0 表示该版本快照内嵌了团队定义，
    发布产品自带成员重建指令；0 = 仅主理人配置（历史版本）。"""
    team_members: int = 0


class VersionDetail(VersionBrief):
    data: dict


class AgentVersionStatus(BaseModel):
    agent_id: str
    frozen: bool
    current_version: int | None
    latest_version: int | None
    versions: list[VersionBrief] = []


class FreezeRequest(BaseModel):
    label: str = ""


agent_version_router = APIRouter(tags=["agent-version"])


def _status(store: AgentVersionStore, agent_id: str) -> AgentVersionStatus:
    rec = store.record(agent_id)
    return AgentVersionStatus(
        agent_id=agent_id,
        frozen=rec["frozen"],
        current_version=rec["current_version"],
        latest_version=rec["versions"][-1]["version"] if rec["versions"] else None,
        versions=[
            VersionBrief(
                version=v["version"],
                created_at=v.get("created_at", ""),
                label=v.get("label", ""),
                team_members=len(
                    ((v.get("data") or {}).get("team_blueprint") or {})
                    .get("members", []),
                ),
            )
            for v in rec["versions"]
        ],
    )


async def _fetch_agent_data(agent_id: str, user_id: str) -> dict:
    """经官方端点拉取 agent 当前配置（官方无单查端点，列表过滤）。

    走 ``_call_official``（未包装的官方 app）：响应是纯官方结构，
    不含中间件注入字段；data 即干净的 AgentData。
    """
    r = await _call_official("GET", "/agent/", user_id)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"agent 列表拉取失败: HTTP {r.status_code}")
    for a in r.json().get("agents", []):
        if isinstance(a, dict) and a.get("id") == agent_id:
            return a.get("data") or {}
    raise HTTPException(status_code=404, detail="智能体不存在")


def _require_user(request: Request) -> str:
    user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return user_id


# ── 团队图纸（2026-09-09 方案 A：发版快照内嵌团队定义） ─────────────────


async def collect_team_blueprint(
    user_id: str, agent_id: str, storage,
) -> dict | None:
    """主理人 agent → 团队图纸（团队名/宗旨 + 成员定义）。

    选队规则：该 agent 领导的团队中，优先选**存活**的（leader 会话
    仍绑定），再按 updated_at 取最新——用户培养的"当前版本团队"。
    解散残留（软解散后 team 记录仍在）不选，除非从无存活团队。

    成员职责提取（与前端 memberRole 同源逻辑）：
    - 邀请成员：``invite_config.invite_description``；
    - AgentCreate 成员：官方模板 system_prompt 的 ``Your role:`` 段
      （职责即 AgentCreate 的 description 参数，重建时原样回传）。
    """
    try:
        teams = await storage.list_teams(user_id)
    except Exception:  # noqa: BLE001 — 无团队环境（测试栈等）
        return None
    led = [t for t in teams if getattr(t, "leader_agent_id", None) == agent_id]
    if not led:
        return None

    async def _alive(team) -> bool:
        try:
            sess = await storage.get_session(
                user_id, agent_id, team.session_id,
            )
            return sess is not None and getattr(sess, "team_id", None) == team.id
        except Exception:  # noqa: BLE001
            return False

    alive = [t for t in led if await _alive(t)]
    pool = alive or led
    team = max(
        pool,
        key=lambda t: getattr(t, "updated_at", "") or "",
    )

    tdata = getattr(team, "data", None)
    members_out: list[dict] = []
    for m in getattr(tdata, "members", None) or []:
        m_agent_id = m.get("agent_id") if isinstance(m, dict) else m.agent_id
        if not m_agent_id:
            continue
        try:
            rec = await storage.get_agent(user_id, m_agent_id)
        except Exception:  # noqa: BLE001 — 成员记录缺失（历史级联）
            continue
        if rec is None:
            continue
        raw = getattr(rec, "config", None) or getattr(rec, "data", None) or {}
        # 官方 AgentRecord.data 是 pydantic 模型；测试 duck-typing 可能
        # 是 dict 或 dataclass——归一成 dict
        if hasattr(raw, "model_dump"):
            cfg = raw.model_dump()
        elif isinstance(raw, dict):
            cfg = raw
        else:
            cfg = {
                "name": getattr(raw, "name", ""),
                "system_prompt": getattr(raw, "system_prompt", ""),
                "invite_config": getattr(raw, "invite_config", None),
            }
        name = cfg.get("name") or (m_agent_id[:8])
        # 职责：invite_description 优先，其次 system_prompt 的 Your role 段
        desc = ""
        inv = cfg.get("invite_config") or {}
        if isinstance(inv, dict):
            desc = inv.get("invite_description") or ""
        sp = cfg.get("system_prompt") or ""
        if not desc:
            mrole = re.search(
                r"Your role:\s*(.*?)(?=\n\nYou communicate|\Z)", sp, re.S,
            )
            if mrole:
                desc = mrole.group(1).strip()
        members_out.append({
            "agent_id": m_agent_id,
            "name": name,
            "description": desc,
            "system_prompt": sp,
        })

    return {
        "team_name": getattr(tdata, "name", "") or "",
        "team_description": getattr(tdata, "description", "") or "",
        "members": members_out,
    }


def _blueprint_prompt_section(bp: dict) -> str:
    """图纸 → 追加到产品 system_prompt 的章节（发布注入用）。

    只注入 name + description（AgentCreate 的参数即这两项）；
    完整成员 system_prompt 留在快照里做溯源，不进提示词（官方
    模板会自动包装，重复注入反而污染）。
    """
    lines = [
        "",
        "## 你的专家团队（发版快照 · 培育成果）",
        f"你在团队「{bp.get('team_name') or '工作组'}」中培养并实战验证过以下专家分工"
        f"（团队宗旨：{bp.get('team_description') or '协作完成复杂任务'}）。",
        "当任务需要组队时，**不要即兴创建成员**，必须用 AgentCreate 按以下定义重建",
        "（名字与职责原样传入，保持一致）：",
        "",
    ]
    for m in bp.get("members") or []:
        lines.append(f"- **{m.get('name', '成员')}**：{m.get('description', '')}")
    lines += [
        "",
        "重建后按任务需要分派具体工作（AgentCreate 的 prompt 参数）。这些职责分工"
        "经过实战验证，是本服务的核心能力资产；除非用户明确要求调整，不要改动成员定义。",
    ]
    return "\n".join(lines)


async def sync_preset_team(user_id: str, agent_id: str, blueprint, storage) -> bool:
    """发版收图纸后回写预置团队名单（2026-09-09：团队随版本走）。

    用户需求：主理人发版（含团队图纸）后，基于该版本开新会话时团队
    要跟过去。团队实体本身不随版本走（在册状态绑定运行时会话），但
    **预置名单**可以：成员 agent 在软解散设计下保留，把图纸成员回写
    为 leader 的预置名单（leader_teams.json）+ 提示词名单段——新会话
    右侧立刻显示成员名单，主理人发任务时 AgentInvite 邀请重建团队。

    图纸无成员（无在册团队/全删了）时不动已有名单（历史资产保留）。
    提示词写走 ``_call_official``（未包装官方 app）——发版封板状态也
    能写，与 restore_version 同款授权语义。
    """
    if not blueprint or not blueprint.get("members"):
        return False
    from .agent_type import AgentTypeStore  # noqa: PLC0415
    from .leader_team import (  # noqa: PLC0415
        LeaderTeamStore,
        build_team_section,
        strip_team_section,
    )

    if AgentTypeStore().load().get(agent_id) != "leader":
        return False
    members: list[dict] = []
    for m in blueprint["members"]:
        aid = m.get("agent_id") or ""
        if not aid:
            continue
        try:
            rec = await storage.get_agent(user_id, aid)
        except Exception:  # noqa: BLE001 — 成员记录缺失（历史级联）
            continue
        if rec is None:
            continue  # 成员已删：不进名单（邀请死 id 会失败）
        members.append({
            "id": aid,
            "name": m.get("name") or aid[:8],
            "description": m.get("description") or "",
        })
    if not members:
        return False
    LeaderTeamStore().set(agent_id, [m["id"] for m in members])
    # 提示词名单段注入（strip 旧段再写新段，SOP 段在名单段之前不受影响）
    record = await storage.get_agent(user_id, agent_id)
    sp = ((getattr(record, "data", None) and record.data.system_prompt) or "").strip()
    if not sp:
        # leader 记录缺失/空提示词：只回写名单，跳过提示词注入——
        # 防御性短路（防止把提示词覆盖成纯名单段）
        _logger.warning("预置名单回写：leader %s 提示词缺失，跳过注入", agent_id)
        return True
    new_sp = strip_team_section(sp) + "\n" + build_team_section(members)
    r = await _call_official(
        "PATCH", f"/agent/{agent_id}", user_id,
        json_body={"system_prompt": new_sp},
    )
    if r.status_code not in (200, 204):
        _logger.warning("预置名单提示词注入失败 %s: HTTP %s", agent_id, r.status_code)
        return False
    return True


async def _require_agent_visible(agent_id: str, request: Request | None) -> None:
    """版本信息含提示词快照：仅对 owner 或被共享账号可见（2026-09-07 共享 v1）。

    之前版本端点只认 agent_id（UUID 猜不中即安全），共享上线后
    必须显式校验——避免未授权账号凭 id 探读提示词资产。
    """
    if request is None:
        return  # 进程内调用（team_archive 等 _call_official 会带头）
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        # 无存储环境（独立测试栈等）：退化为仅凭 agent_id 的原行为
        return
    user_id = _require_user(request)
    # 自己的（含团队成员）或被共享的都放行
    if await storage.get_agent(user_id, agent_id) is not None:
        return
    policy = getattr(request.app.state, "resource_access_policy", None)
    if policy is not None:
        refs = await policy.list_accessible(user_id, ResourceKind.AGENT, storage)
        if any(r.resource_id == agent_id for r in refs):
            return
    raise HTTPException(status_code=404, detail="智能体不存在")


@agent_version_router.post(
    "/agent/{agent_id}/freeze",
    response_model=AgentVersionStatus,
    summary="冻结智能体：当前配置封板为版本号，拦截后续修改",
)
async def freeze_agent(agent_id: str, body: FreezeRequest | None = None, request: Request = None) -> AgentVersionStatus:
    user_id = _require_user(request)
    data = await _fetch_agent_data(agent_id, user_id)
    # 方案 A（2026-09-09）：主理人发版快照内嵌团队定义——培养的是
    # 大A+Team 整体资产，快照只有主理人壳子 = 发布丢掉团队
    blueprint = await collect_team_blueprint(
        user_id, agent_id, getattr(request.app.state, "storage", None),
    ) if getattr(request.app.state, "storage", None) is not None else None
    store = AgentVersionStore()
    entry = store.add_version(
        agent_id, data, (body.label if body else "") or "",
        team_blueprint=blueprint,
    )
    rec = store.record(agent_id)
    rec["frozen"] = True
    rec["current_version"] = entry["version"]
    store.save(agent_id, rec)
    # 团队随版本走：图纸回写预置名单（新会话右侧显示成员、主理人可邀请重建）
    if blueprint is not None:
        await sync_preset_team(
            user_id, agent_id, blueprint,
            getattr(request.app.state, "storage", None),
        )
    return _status(store, agent_id)


@agent_version_router.post(
    "/agent/{agent_id}/unfreeze",
    response_model=AgentVersionStatus,
    summary="解冻智能体：开放模式，恢复可编辑",
)
async def unfreeze_agent(agent_id: str, request: Request = None) -> AgentVersionStatus:
    _require_user(request)
    store = AgentVersionStore()
    rec = store.record(agent_id)
    if not rec["versions"]:
        raise HTTPException(status_code=404, detail="该智能体没有版本记录，无需解冻")
    rec["frozen"] = False
    store.save(agent_id, rec)
    return _status(store, agent_id)


@agent_version_router.post(
    "/agent/{agent_id}/save-version",
    response_model=AgentVersionStatus,
    summary="保存当前配置为新版本（开放模式下迭代满意后手动存版）",
)
async def save_version(agent_id: str, body: FreezeRequest | None = None, request: Request = None) -> AgentVersionStatus:
    user_id = _require_user(request)
    data = await _fetch_agent_data(agent_id, user_id)
    # 方案 A：与 freeze 同源——每次发版重收团队图纸（团队迭代了
    # 图纸跟着更新，自我修复）
    blueprint = await collect_team_blueprint(
        user_id, agent_id, getattr(request.app.state, "storage", None),
    ) if getattr(request.app.state, "storage", None) is not None else None
    store = AgentVersionStore()
    # force=True：显式发版总是新增（配置未变也产生新版本号）
    entry = store.add_version(
        agent_id, data, (body.label if body else "") or "", force=True,
        team_blueprint=blueprint,
    )
    rec = store.record(agent_id)
    rec["current_version"] = entry["version"]
    store.save(agent_id, rec)
    # 团队随版本走：图纸回写预置名单（与 freeze 同源）
    if blueprint is not None:
        await sync_preset_team(
            user_id, agent_id, blueprint,
            getattr(request.app.state, "storage", None),
        )
    return _status(store, agent_id)


@agent_version_router.get(
    "/agent/{agent_id}/versions",
    response_model=AgentVersionStatus,
    summary="版本列表（不含快照正文）",
)
async def list_versions(agent_id: str, request: Request = None) -> AgentVersionStatus:
    await _require_agent_visible(agent_id, request)
    return _status(AgentVersionStore(), agent_id)


# ── 复制（增删改查之"增"：从现有智能体派生新个体） ──────────────────────

class DuplicateRequest(BaseModel):
    """复制智能体：可选源版本快照（默认当前配置）。"""

    name: str = Field(default="", description="新智能体名（默认「原名 副本」）")
    version: int | None = Field(
        default=None,
        description="源版本号；None = 当前配置。复制历史版本可从任意节点分叉",
    )


async def duplicate_agent_core(
    agent_id: str,
    user_id: str,
    name: str,
    version: int | None,
    storage,
    team_mode: str = "blueprint",
) -> dict:
    """复制核心：读源配置（或版本快照）→ 官方创建新 agent。

    发布（agent_share.publish）与用户手动复制共用此函数——
    「发布 = 从版本快照复制出对外产品」。

    ``storage``：官方 storage（按 owner 键控）——所有权校验必须走
    get_agent 直查，不能用 /agent/ 列表（列表会合并被共享的他人
    智能体，那样就能复制别人的了）。

    ``team_mode``（2026-09-09 发布形态）：
    - ``blueprint``（默认）：固定团队——快照图纸注入产品提示词，
      组队时按定义原样重建成员（培育成果随版本走）；
    - ``auto``：自动组建——不注入名单，产品保留 leader 组队能力
      （AgentCreate），按任务即兴组队。发布"不带团队的主理人"用此模式。
    """
    record = await storage.get_agent(user_id, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="智能体不存在（或不在你的账号下）")
    if getattr(record, "source", "") == "team":
        raise HTTPException(
            status_code=400,
            detail="团队成员智能体随团队生命周期管理，不支持复制",
        )
    data = await _fetch_agent_data(agent_id, user_id)
    if version is not None:
        entry = AgentVersionStore().get_version(agent_id, version)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
        data = {**data, **(entry.get("data") or {})}
    # 方案 A（2026-09-09）：快照带团队图纸 → 注入产品提示词。
    # 图纸字段本身必须剥离（官方 AgentData 无此字段，带着会被
    # pydantic 拒掉）；发布的产品按图纸重建团队成员。
    # team_mode=auto：不注入名单（自动组建形态）。
    blueprint = data.pop("team_blueprint", None)
    new_name = (name or f"{data.get('name', '智能体')} 副本").strip()[:100] or "未命名智能体"
    payload = {**data, "name": new_name}
    if team_mode != "auto" and blueprint and blueprint.get("members"):
        payload["system_prompt"] = (
            (payload.get("system_prompt") or "").rstrip()
            + "\n" + _blueprint_prompt_section(blueprint)
        )
    # 类型跟随源（leader/member）。_call_official 走未包装官方 app，
    # AgentTypeMiddleware 不在链上——body 里的 agent_type 无人剥离存映射
    # （2026-09-09 bug：发布产品全被标默认小A → 无组队能力，方案A
    # 注入的团队图纸无法兑现）。正确做法：创建成功后直写类型表。
    from .agent_type import AgentTypeStore  # noqa: PLC0415
    atype = AgentTypeStore().load().get(agent_id)
    r = await _call_official("POST", "/agent/", user_id, json_body=payload)
    if r.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"官方创建失败: HTTP {r.status_code} {r.text[:200]}",
        )
    new_id = (r.json() or {}).get("agent_id") or (r.json() or {}).get("id") or ""
    if not new_id:
        raise HTTPException(status_code=502, detail="官方创建失败：响应缺 id")
    if atype:
        AgentTypeStore().set(new_id, atype)
    return {
        "agent_id": new_id,
        "name": new_name,
        "source_agent_id": agent_id,
        "source_version": version,
    }


@agent_version_router.post(
    "/agent/{agent_id}/duplicate",
    summary="复制智能体（可选版本快照；从任意版本节点分叉出新个体）",
)
async def duplicate_agent(
    agent_id: str,
    body: DuplicateRequest | None = None,
    request: Request = None,
) -> dict:
    user_id = _require_user(request)
    req = body or DuplicateRequest()
    return await duplicate_agent_core(
        agent_id, user_id, req.name, req.version,
        request.app.state.storage,
    )


@agent_version_router.get(
    "/agent/{agent_id}/versions/{version}",
    response_model=VersionDetail,
    summary="版本详情（含配置快照）",
)
async def get_version(agent_id: str, version: int, request: Request = None) -> VersionDetail:
    await _require_agent_visible(agent_id, request)
    entry = AgentVersionStore().get_version(agent_id, version)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    return VersionDetail(
        version=entry["version"],
        created_at=entry.get("created_at", ""),
        label=entry.get("label", ""),
        team_members=len(
            ((entry.get("data") or {}).get("team_blueprint") or {})
            .get("members", []),
        ),
        data=entry.get("data") or {},
    )


@agent_version_router.post(
    "/agent/{agent_id}/versions/{version}/restore",
    response_model=AgentVersionStatus,
    summary="恢复到历史版本（显式人工操作 = 授权，冻结中也可执行）",
)
async def restore_version(agent_id: str, version: int, request: Request = None) -> AgentVersionStatus:
    user_id = _require_user(request)
    store = AgentVersionStore()
    entry = store.get_version(agent_id, version)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    # 经官方端点直写（_official_app 未包装拦截链），冻结中也不会被
    # 自家中间件 403 拦住——这就是"得到授权后的更新"
    # 图纸字段剥离（官方 AgentData 无此字段；恢复的是主理人配置，
    # 团队实体本身不随版本恢复——但预置名单随图纸同步（2026-09-09：
    # 恢复到含团队图纸的版本，团队要跟过去））
    blueprint = (entry.get("data") or {}).get("team_blueprint")
    restore_data = {
        k: v for k, v in (entry.get("data") or {}).items()
        if k != "team_blueprint"
    }
    r = await _call_official(
        "PATCH", f"/agent/{agent_id}", user_id,
        json_body=restore_data,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=f"版本恢复写入失败: HTTP {r.status_code}",
        )
    # 团队随版本走：恢复的快照图纸回写预置名单（新会话团队跟过去）
    storage = getattr(request.app.state, "storage", None)
    if blueprint is not None and storage is not None:
        await sync_preset_team(user_id, agent_id, blueprint, storage)
    rec = store.record(agent_id)
    rec["current_version"] = version
    store.save(agent_id, rec)
    return _status(store, agent_id)


# ── 中间件 ─────────────────────────────────────────────────────────────

async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class AgentVersionMiddleware:
    """在官方 agent API 上叠加版本封板：冻结拦截 PATCH、GET 注入状态。"""

    def __init__(self, app, store: AgentVersionStore | None = None) -> None:
        self.app = app
        self.store = store or AgentVersionStore()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        p = path.rstrip("/") or "/"

        # 只拦 /agent 精确路径与 /agent/{id} 单段；
        # /agent/{id}/freeze 等多段路由透传给 router
        agent_id = None
        if p == "/agent":
            if method not in ("GET",):
                await self.app(scope, receive, send)
                return
        elif p.startswith("/agent/"):
            rest = p[len("/agent/"):]
            if not rest or "/" in rest:
                await self.app(scope, receive, send)
                return
            agent_id = rest
        else:
            await self.app(scope, receive, send)
            return

        if method == "PATCH" and agent_id is not None:
            rec = self.store.record(agent_id)
            if rec["frozen"]:
                v = rec["current_version"]
                await _send_json(send, 403, {
                    "detail": (
                        f"该智能体已冻结（版本 v{v}），自我迭代已停止。"
                        "如需修改，请先解冻（开放模式）或在版本页恢复历史版本。"
                    ),
                })
                return
            await self.app(scope, receive, send)
            return

        if method == "DELETE" and agent_id is not None:
            await self._run_delete(scope, receive, send, agent_id)
            return

        # GET /agent/（列表）→ 注入版本状态
        await self._run_inject(scope, receive, send)
        return

    async def _run_delete(self, scope, receive, send, agent_id: str) -> None:
        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                status = (state["start"] or {}).get("status", 500)
                if status in (200, 204):
                    self.store.delete(agent_id)
                await _replay(state, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _run_inject(self, scope, receive, send) -> None:
        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(state["chunks"])
                status = (state["start"] or {}).get("status", 500)
                if status == 200:
                    body = self._inject_body(body)
                await _replay(state, send, override_body=body)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _inject_body(self, body: bytes) -> bytes:
        """把 version 状态写进列表响应的每个 agent，失败原样返回。"""
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return body
        agents = data.get("agents") if isinstance(data, dict) else None
        if not isinstance(agents, list):
            return body
        for a in agents:
            if not (isinstance(a, dict) and a.get("id")):
                continue
            rec = self.store.record(a["id"])
            a["version"] = {
                "frozen": rec["frozen"],
                "current_version": rec["current_version"],
                "latest_version": (
                    rec["versions"][-1]["version"] if rec["versions"] else None
                ),
            }
        try:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            _logger.warning("version 注入失败: %s", e)
            return body
