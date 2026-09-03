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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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

    def add_version(self, agent_id: str, data: dict, label: str = "") -> dict:
        """追加版本快照；与最新版本内容一致时复用（冻结→解冻→再冻结
        不产生冗余版本号）。返回版本条目。
        """
        rec = self.record(agent_id)
        payload = {k: data[k] for k in CONFIG_FIELDS if k in data}
        if rec["versions"] and rec["versions"][-1].get("data") == payload:
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


@agent_version_router.post(
    "/agent/{agent_id}/freeze",
    response_model=AgentVersionStatus,
    summary="冻结智能体：当前配置封板为版本号，拦截后续修改",
)
async def freeze_agent(agent_id: str, body: FreezeRequest | None = None, request: Request = None) -> AgentVersionStatus:
    user_id = _require_user(request)
    data = await _fetch_agent_data(agent_id, user_id)
    store = AgentVersionStore()
    entry = store.add_version(agent_id, data, (body.label if body else "") or "")
    rec = store.record(agent_id)
    rec["frozen"] = True
    rec["current_version"] = entry["version"]
    store.save(agent_id, rec)
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
    store = AgentVersionStore()
    entry = store.add_version(agent_id, data, (body.label if body else "") or "")
    rec = store.record(agent_id)
    rec["current_version"] = entry["version"]
    store.save(agent_id, rec)
    return _status(store, agent_id)


@agent_version_router.get(
    "/agent/{agent_id}/versions",
    response_model=AgentVersionStatus,
    summary="版本列表（不含快照正文）",
)
async def list_versions(agent_id: str) -> AgentVersionStatus:
    return _status(AgentVersionStore(), agent_id)


@agent_version_router.get(
    "/agent/{agent_id}/versions/{version}",
    response_model=VersionDetail,
    summary="版本详情（含配置快照）",
)
async def get_version(agent_id: str, version: int) -> VersionDetail:
    entry = AgentVersionStore().get_version(agent_id, version)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version} 不存在")
    return VersionDetail(
        version=entry["version"],
        created_at=entry.get("created_at", ""),
        label=entry.get("label", ""),
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
    r = await _call_official(
        "PATCH", f"/agent/{agent_id}", user_id,
        json_body=entry.get("data") or {},
    )
    if r.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=f"版本恢复写入失败: HTTP {r.status_code}",
        )
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
