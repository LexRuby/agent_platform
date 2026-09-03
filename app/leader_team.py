"""主理人（大A）预置团队成员：创建 leader 时的成员名单叠加层。

- :class:`LeaderTeamStore`：``data/leader_teams.json`` 存
  ``{leader_agent_id: [member_agent_id, ...]}``
- :class:`LeaderTeamMiddleware`：拦截 ``/agent`` 路由——
  - ``POST``：请求体剥离 ``team_members``（id 数组，或
    ``{id, name, description}`` 对象数组），把名单以标准段落追加进
    ``system_prompt``（运行时生效方式：leader 提示词自带名单，官方
    ``AgentInvite`` 机制照常工作）；响应拿 id 后存 sidecar
  - ``PATCH``：同 POST（id 取自路径），按段落标记整段重写
  - ``GET /agent/``：每个有名单的 agent 注入 ``team_members`` 供前端回显
  - ``DELETE /agent/{id}``：清理 sidecar
- ``POST /agent/recommend-members``：基于 system_prompt / 任务议题 +
  在册 member 清单，调大模型推荐（LLM 失败降级返回空列表）。
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from .agent_type import (
    LEADER,
    AgentTypeStore,
    _make_receive,
    _read_body,
    _replay,
)
from .llm_utils import llm_chat_json

_logger = logging.getLogger("agentforge.leader_team")

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILE = _BASE_DIR / "data" / "leader_teams.json"

# 注入 system_prompt 的名单段落标记（PATCH 时按标记整段替换）
SECTION_MARKER = "## 预置团队成员"


def _teams_file() -> Path:
    return Path(
        os.environ.get("AGENTFORGE_LEADER_TEAMS_FILE", str(_DEFAULT_FILE)),
    )


class LeaderTeamStore:
    """leader_agent_id → [member_agent_id] 的文件持久化。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _teams_file()

    def load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                k: [m for m in v if isinstance(m, str)]
                for k, v in data.items()
                if isinstance(v, list)
            }
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            _logger.warning("leader 团队文件损坏（按空处理）%s: %s", self.path, e)
            return {}

    def get(self, leader_id: str) -> list[str]:
        return list(self.load().get(leader_id, []))

    def set(self, leader_id: str, member_ids: list[str]) -> None:
        mapping = self.load()
        members = [m for m in dict.fromkeys(member_ids) if isinstance(m, str)]
        if not members:
            mapping.pop(leader_id, None)
        else:
            mapping[leader_id] = members
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def delete(self, leader_id: str) -> None:
        mapping = self.load()
        if leader_id in mapping:
            del mapping[leader_id]
            self.path.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def build_team_section(members: list[dict]) -> str:
    """把成员名单渲染成注入 system_prompt 的标准段落。"""
    lines = [
        "",
        SECTION_MARKER,
        "以下是为你预置的团队成员（小A）。执行团队任务时：",
        "1. 优先用 AgentInvite 邀请下列成员加入团队；",
        "2. 名单不构成限制——任务需要时仍可邀请其他在册 agent，"
        "或用 AgentCreate 创建新的临时成员；",
        "3. 成员汇报会以 team-message 形式到达，注意整合他们的产出。",
    ]
    for m in members:
        desc = (m.get("description") or "").strip()
        lines.append(f"- {m['name']}" + (f"：{desc}" if desc else ""))
    return "\n".join(lines)


def strip_team_section(system_prompt: str) -> str:
    """移除 system_prompt 中已有的名单段落（重写前调用）。"""
    if SECTION_MARKER not in system_prompt:
        return system_prompt.rstrip()
    head, _, _ = system_prompt.partition(SECTION_MARKER)
    return head.rstrip()


def extract_team_members(body: bytes) -> tuple[bytes, list[dict] | None]:
    """从 JSON 请求体剥离 team_members。

    支持元素为纯 id 字符串或 ``{id, name?, description?}`` 对象；
    统一归一为对象数组（name 缺省取 id 前 8 位）。
    非 JSON / 无该键返回 ``(原 body, None)``。
    """
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return body, None
    if not isinstance(data, dict) or "team_members" not in data:
        return body, None
    raw = data.pop("team_members")
    if not isinstance(raw, list):
        return body, None
    out = []
    for m in raw:
        if isinstance(m, str) and m:
            out.append({"id": m, "name": m[:8], "description": ""})
        elif isinstance(m, dict) and m.get("id"):
            out.append({
                "id": m["id"],
                "name": m.get("name") or m["id"][:8],
                "description": m.get("description") or "",
            })
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), out


class LeaderTeamMiddleware:
    """在官方 agent API 上叠加 team_members：请求捕获，响应注入。"""

    def __init__(
        self,
        app,
        store: LeaderTeamStore | None = None,
        type_store: AgentTypeStore | None = None,
    ) -> None:
        self.app = app
        self.store = store or LeaderTeamStore()
        self.type_store = type_store or AgentTypeStore()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        p = path.rstrip("/") or "/"

        if p == "/agent" and method == "POST":
            await self._run_post(scope, receive, send)
            return
        if p == "/agent" and method == "GET":
            await self._run_inject(scope, receive, send)
            return
        if p.startswith("/agent/"):
            rest = p[len("/agent/"):]
            if rest and "/" not in rest:
                # 注意：/agent/recommend-members（POST）等其余方法透传给 router
                if method == "PATCH":
                    await self._run_patch(scope, receive, send, rest)
                    return
                if method == "DELETE":
                    await self._run_delete(scope, receive, send, rest)
                    return
        await self.app(scope, receive, send)

    def _validate_members(self, members: list[dict]) -> list[dict]:
        """过滤非法项：id 为空或引用了 leader 的成员丢弃（默认 member 放行）。"""
        valid = []
        for m in members:
            if not m.get("id"):
                continue
            # type_store 未标记的默认 member（放行）；标记为 leader 的拒绝
            if self.type_store.get(m["id"]) == LEADER:
                _logger.warning("忽略 leader 作为团队成员: %s", m["id"])
                continue
            valid.append(m)
        return valid

    async def _run_post(self, scope, receive, send) -> None:
        body = await _read_body(receive)
        new_body, members = extract_team_members(body)
        receive = _make_receive(new_body)
        if members is None:
            await self.app(scope, receive, send)
            return
        try:
            data = json.loads(new_body)
        except Exception:  # noqa: BLE001
            data = {}
        valid = self._validate_members(members) if data.get("agent_type") == LEADER else []
        if valid:
            data["system_prompt"] = (
                (data.get("system_prompt") or "").rstrip()
                + "\n"
                + build_team_section(valid)
            )
            receive = _make_receive(
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
            )
        ids = [m["id"] for m in valid]

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
                if status in (200, 201) and ids:
                    try:
                        resp = json.loads(b"".join(state["chunks"]))
                        new_id = resp.get("id") or resp.get("agent", {}).get("id")
                        if new_id:
                            self.store.set(new_id, ids)
                    except Exception as e:  # noqa: BLE001
                        _logger.warning("team_members 存储失败: %s", e)
                await _replay(state, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _run_patch(self, scope, receive, send, leader_id: str) -> None:
        body = await _read_body(receive)
        new_body, members = extract_team_members(body)
        receive = _make_receive(new_body)
        if members is None:
            await self.app(scope, receive, send)
            return
        try:
            data = json.loads(new_body)
        except Exception:  # noqa: BLE001
            data = {}
        valid = self._validate_members(members)
        if valid:
            base_prompt = strip_team_section(data.get("system_prompt") or "")
            data["system_prompt"] = (
                base_prompt + "\n" + build_team_section(valid)
            )
        else:
            # 空名单：清 sidecar；若带 system_prompt 则去掉旧段落
            if "system_prompt" in data:
                data["system_prompt"] = strip_team_section(
                    data.get("system_prompt") or "",
                )
            self.store.delete(leader_id)
        receive = _make_receive(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        )
        ids = [m["id"] for m in valid]

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
                if status in (200, 204) and ids:
                    self.store.set(leader_id, ids)
                await _replay(state, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _run_delete(self, scope, receive, send, leader_id: str) -> None:
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
                    self.store.delete(leader_id)
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
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return body
        agents = data.get("agents") if isinstance(data, dict) else None
        if not isinstance(agents, list):
            return body
        mapping = self.store.load()
        for a in agents:
            if isinstance(a, dict) and a.get("id") in mapping:
                a["team_members"] = mapping[a["id"]]
        try:
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            _logger.warning("team_members 注入失败: %s", e)
            return body


# ── AI 推荐成员 ─────────────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    """推荐请求：主理人设定 + 任务议题 + 在册 member 候选清单。

    members 由前端从 ``GET /agent/`` 取（id/name/description），
    后端只对 member 类型做服务端过滤（不信任前端类型标注）。
    """

    system_prompt: str | None = None
    task_topic: str | None = None
    members: list[dict] = []


class RecommendResponse(BaseModel):
    recommendations: list[dict]
    fallback: bool = False
    reason: str | None = None


leader_team_router = APIRouter(tags=["leader-team"])


async def _recommend_llm(
    context_text: str, candidates: list[dict],
) -> list[dict]:
    """调 LLM 从候选中挑选相关成员。返回 [{id, name, reason}]。"""
    cand_lines = [
        f'- id={c["id"]} name={c["name"]}：{c.get("description") or "（无描述）"}'
        for c in candidates
    ]
    prompt = (
        "以下是一个团队主理人（leader agent）的设定与任务议题：\n"
        f"{context_text}\n\n"
        "在册的团队成员（member agent）候选：\n"
        f"{chr(10).join(cand_lines)}\n\n"
        "请从中挑选最多 5 个与任务最相关的成员，输出 JSON：\n"
        '{"recommendations": [{"id": "<候选id>", "reason": "<一句话推荐理由>"}]}\n'
        "只输出 JSON，不要输出其他内容。若无合适成员，recommendations 为空数组。"
    )
    result = await llm_chat_json(prompt, system="你是团队组建顾问，擅长根据任务匹配专家。")
    recs = result.get("recommendations") if isinstance(result, dict) else None
    if not isinstance(recs, list):
        raise ValueError("LLM 未返回 recommendations 数组")
    by_id = {c["id"]: c for c in candidates}
    out = []
    for r in recs[:5]:
        if not isinstance(r, dict) or r.get("id") not in by_id:
            continue
        out.append({
            "id": r["id"],
            "name": by_id[r["id"]]["name"],
            "description": by_id[r["id"]].get("description") or "",
            "reason": r.get("reason") or "",
        })
    return out


@leader_team_router.post(
    "/agent/recommend-members",
    response_model=RecommendResponse,
    summary="基于主理人设定/任务议题，推荐在册的小A 成员",
)
async def recommend_members(body: RecommendRequest) -> RecommendResponse:
    context_text = " ".join(
        p.strip()
        for p in (body.system_prompt, body.task_topic)
        if p and p.strip()
    )
    if not context_text:
        return RecommendResponse(
            recommendations=[], reason="empty-input",
        )

    # 服务端过滤：只保留类型为 member 的候选（type_store 未标记默认 member）。
    # 候选元素兼容 AgentView 结构（name 在 data.name）与扁平 {id, name} 结构。
    type_store = AgentTypeStore()

    def _cand_name(c: dict) -> str:
        data = c.get("data")
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
        return c.get("name") or c.get("id", "")[:8]

    candidates = [
        {
            "id": c.get("id", ""),
            "name": _cand_name(c),
            "description": c.get("description") or "",
        }
        for c in body.members
        if c.get("id") and type_store.get(c["id"]) != LEADER
    ]
    if not candidates:
        return RecommendResponse(
            recommendations=[], reason="no-candidates",
        )

    try:
        recs = await _recommend_llm(context_text, candidates)
    except Exception as e:  # noqa: BLE001 - LLM 失败降级
        _logger.warning("成员推荐 LLM 调用失败: %s", e)
        return RecommendResponse(
            recommendations=[], fallback=True, reason=f"llm-error: {e}",
        )
    return RecommendResponse(recommendations=recs)
