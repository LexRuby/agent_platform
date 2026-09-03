"""团队封档：任务总结归档 → 可复用的验证过的工作流团队。

流程：
1. ``POST /team-archive/summarize``：拉取 leader 会话全部消息，
   调 LLM 生成归档草稿（任务总结 + 工作流步骤 + 建议注册的新 agent），
   返回给人工编辑（不落盘）
2. 人工确认后 ``POST /team-archive``：落盘 ``data/team_archives.json``；
   对 ``new_agents`` 逐个进程内调用官方 ``POST /agent/``
   （带 ``agent_type: member``）注册进 agent 库
3. ``GET /team-archive``：列表；``GET /team-archive/{id}``：详情

身份：AuthMiddleware 已把登录用户写入 ``X-User-ID`` 请求头（服务端
覆盖，可信），本模块直接读取。进程内调用官方端点用 httpx ASGI
transport（不打端口、不需 cookie）。
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .llm_utils import llm_chat_json

_logger = logging.getLogger("agentforge.team_archive")

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILE = _BASE_DIR / "data" / "team_archives.json"


def _archive_file() -> Path:
    return Path(
        os.environ.get("AGENTFORGE_TEAM_ARCHIVES_FILE", str(_DEFAULT_FILE)),
    )


class ArchiveStore:
    """封档记录的文件持久化。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _archive_file()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            _logger.warning("封档文件损坏（按空处理）%s: %s", self.path, e)
            return []

    def get(self, archive_id: str) -> dict | None:
        return next((a for a in self.load() if a.get("id") == archive_id), None)

    def add(self, record: dict) -> None:
        records = self.load()
        records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ── 请求/响应模型 ──────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    agent_id: str
    session_id: str


class NewAgentDraft(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""


class SummarizeResponse(BaseModel):
    summary: str
    workflow_steps: list[str]
    new_agents: list[NewAgentDraft]
    fallback: bool = False
    reason: str | None = None


class ArchiveCreateRequest(BaseModel):
    name: str
    summary: str
    workflow_steps: list[str] = []
    source_agent_id: str | None = None
    source_session_id: str | None = None
    team_members: list[dict] = []  # [{id, name}]
    new_agents: list[NewAgentDraft] = []


class ArchiveResponse(BaseModel):
    id: str
    name: str
    summary: str
    workflow_steps: list[str]
    team_members: list[dict]
    new_agents: list[dict]
    created_at: str
    source_agent_id: str | None = None
    source_session_id: str | None = None


team_archive_router = APIRouter(tags=["team-archive"])


async def _call_official(
    method: str, path: str, user_id: str, json_body: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    """进程内调用官方端点（httpx ASGI transport，带 X-User-ID）。"""
    from agent_service_app import _official_app

    transport = httpx.ASGITransport(app=_official_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://internal", timeout=30.0,
    ) as c:
        return await c.request(
            method, path, json=json_body, params=params,
            headers={"X-User-ID": user_id},
        )


def _messages_to_transcript(messages: list[dict]) -> str:
    """把会话消息压成 LLM 可读的文本纪要（截断防超长）。"""
    lines = []
    for m in messages:
        name = m.get("name") or m.get("role", "?")
        parts = []
        for b in m.get("content") or []:
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text", ""))
            elif t == "tool_call":
                inp = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                parts.append(f"[调用工具 {b.get('name')} {inp}]")
            elif t == "tool_result":
                s = b.get("content", "")
                if not isinstance(s, str):
                    s = json.dumps(s, ensure_ascii=False)
                parts.append(f"[工具结果 {b.get('name')}] {s[:300]}")
            elif t == "hint":
                parts.append(f"[团队消息] {str(b.get('hint', ''))[:300]}")
        text = " ".join(p for p in parts if p).strip()
        if text:
            lines.append(f"{name}: {text[:600]}")
    return "\n".join(lines)[:60000]


async def _summarize_llm(transcript: str) -> dict:
    """调 LLM 生成归档草稿。"""
    prompt = (
        "以下是一个团队主理人（leader）与其团队成员协作完成任务的完整对话纪要：\n"
        f"{transcript}\n\n"
        "请生成团队归档草稿，输出 JSON：\n"
        "{\n"
        '  "summary": "任务总结（200字内：做了什么、达成什么结果与共识）",\n'
        '  "workflow_steps": ["抽象出的可复用工作流步骤，每步一句话，'
        '按执行顺序", "..."],\n'
        '  "new_agents": [{"name": "建议注册的新agent名", '
        '"description": "一句话职责", '
        '"system_prompt": "该agent的系统提示词（领域专家设定）"}]\n'
        "}\n"
        "new_agents 只列本次任务中临时创建、值得沉淀复用的角色；"
        "没有则为空数组。只输出 JSON。"
    )
    result = await llm_chat_json(
        prompt, system="你是团队工作流架构师，擅长从协作记录中沉淀可复用流程。",
    )
    if not isinstance(result, dict):
        raise ValueError("LLM 未返回对象")
    return result


def _to_draft(resp: dict) -> SummarizeResponse:
    return SummarizeResponse(
        summary=str(resp.get("summary") or "")[:2000],
        workflow_steps=[str(s) for s in (resp.get("workflow_steps") or [])][:30],
        new_agents=[
            NewAgentDraft(
                name=str(a.get("name") or "").strip()[:100],
                description=str(a.get("description") or "")[:500],
                system_prompt=str(a.get("system_prompt") or "")[:4000],
            )
            for a in (resp.get("new_agents") or [])
            if isinstance(a, dict) and a.get("name")
        ][:10],
    )


@team_archive_router.post(
    "/team-archive/summarize",
    response_model=SummarizeResponse,
    summary="生成归档草稿（LLM 总结，不落盘，返回人工编辑）",
)
async def summarize_session(body: SummarizeRequest, request: Request) -> SummarizeResponse:
    user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    r = await _call_official(
        "GET",
        f"/sessions/{body.session_id}/messages",
        user_id,
        params={"agent_id": body.agent_id, "limit": 500},
    )
    if r.status_code != 200:
        raise HTTPException(
            status_code=404, detail=f"会话不可读: HTTP {r.status_code}",
        )
    transcript = _messages_to_transcript(r.json().get("messages", []))
    if not transcript.strip():
        raise HTTPException(status_code=400, detail="会话无消息可归档")

    try:
        return _to_draft(await _summarize_llm(transcript))
    except Exception as e:  # noqa: BLE001
        _logger.warning("归档总结 LLM 调用失败: %s", e)
        return SummarizeResponse(
            summary="（LLM 总结失败，请人工填写）",
            workflow_steps=[],
            new_agents=[],
            fallback=True,
            reason=f"llm-error: {e}",
        )


@team_archive_router.post(
    "/team-archive",
    response_model=ArchiveResponse,
    summary="确认归档：封档存储 + 新 agent 注册入库",
)
async def create_archive(body: ArchiveCreateRequest, request: Request) -> ArchiveResponse:
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="封档名称不能为空")

    user_id = request.headers.get("X-User-ID", "")

    registered: list[dict] = []
    if body.new_agents and user_id:
        for a in body.new_agents[:10]:
            try:
                r = await _call_official(
                    "POST", "/agent/", user_id,
                    json_body={
                        "name": a.name,
                        "system_prompt": a.system_prompt,
                        "agent_type": "member",
                    },
                )
                r.raise_for_status()
                new_id = r.json().get("id")
                if new_id:
                    registered.append({
                        "id": new_id,
                        "name": a.name,
                        "description": a.description,
                    })
            except Exception as e:  # noqa: BLE001
                _logger.warning("新 agent 注册失败 %s: %s", a.name, e)

    record = {
        "id": uuid.uuid4().hex,
        "name": body.name.strip()[:200],
        "summary": body.summary[:2000],
        "workflow_steps": [str(s) for s in body.workflow_steps][:30],
        "team_members": [
            {"id": m.get("id", ""), "name": m.get("name", "")}
            for m in body.team_members
            if isinstance(m, dict) and m.get("id")
        ][:20],
        "new_agents": registered,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_agent_id": body.source_agent_id,
        "source_session_id": body.source_session_id,
    }
    ArchiveStore().add(record)
    return ArchiveResponse(**record)


@team_archive_router.get(
    "/team-archive",
    summary="封档列表",
)
async def list_archives() -> list[dict]:
    return ArchiveStore().load()


@team_archive_router.get(
    "/team-archive/{archive_id}",
    summary="封档详情",
)
async def get_archive(archive_id: str) -> dict:
    rec = ArchiveStore().get(archive_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="封档不存在")
    return rec
