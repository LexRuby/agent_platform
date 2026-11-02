"""Durable usage records for patent-drafting agents.

Conversation deletion is a user-facing lifecycle operation.  Product
improvement evidence has a different lifecycle, so patent-agent messages are
mirrored into account-scoped Redis hashes before a conversation can be
removed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from .patent_search_tools import PATENT_AGENT_PROFILE_MARKER


_logger = logging.getLogger("agentforge.patent_observations")
_KEY = "agentforge:patent-observation:{user}:{agent}"
_SESSIONS_KEY = "agentforge:patent-observation-sessions:{user}:{agent}"
_PATCHED = False
_storage: Any = None
_PHYSICAL_WORKSPACE_RE = re.compile(
    r"/data/[^/\s]+/AgentScope/agentforge/data/workspaces/"
    r"[a-f0-9]{32}/(?:conversations|projects)/[a-f0-9]{32}",
)

patent_observations_router = APIRouter(tags=["patent-observations"])


def _dump(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(value.model_dump_json())


def _public_value(value: Any) -> Any:
    if isinstance(value, str):
        return _PHYSICAL_WORKSPACE_RE.sub(".", value)
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items()}
    return value


async def _patent_agent(storage: Any, user_id: str, session_id: str) -> tuple[str, str] | None:
    raw = await storage._client.get(f"agentscope:user:{user_id}:session:{session_id}")
    if not raw:
        return None
    session = json.loads(raw)
    agent_id = session.get("agent_id") or ""
    if not agent_id:
        return None
    agent = await storage.get_agent(user_id, agent_id)
    if agent is None or PATENT_AGENT_PROFILE_MARKER not in (agent.data.system_prompt or ""):
        return None
    return agent_id, agent.data.name


async def archive_message(storage: Any, user_id: str, session_id: str, msg: Any) -> bool:
    resolved = await _patent_agent(storage, user_id, session_id)
    if resolved is None:
        return False
    agent_id, agent_name = resolved
    record = _dump(msg)
    message_id = str(record.get("id") or "")
    if not message_id:
        return False
    record["session_id"] = session_id
    record["agent_id"] = agent_id
    record["agent_name"] = agent_name
    key = _KEY.format(user=user_id, agent=agent_id)
    await storage._client.hset(key, message_id, json.dumps(record, ensure_ascii=False))
    await storage._client.sadd(
        _SESSIONS_KEY.format(user=user_id, agent=agent_id),
        session_id,
    )
    return True


def patch_patent_observations(storage: Any) -> None:
    global _PATCHED, _storage
    _storage = storage
    if _PATCHED:
        return
    original = type(storage).upsert_message

    async def observed_upsert(self: Any, user_id: str, session_id: str, msg: Any) -> None:
        await original(self, user_id, session_id, msg)
        try:
            await archive_message(self, user_id, session_id, msg)
        except Exception:  # noqa: BLE001 - observation must never break chat
            _logger.exception("专利小A使用记录保存失败: session=%s", session_id)

    type(storage).upsert_message = observed_upsert
    _PATCHED = True


async def backfill_patent_observations(storage: Any) -> int:
    """Mirror existing patent-agent messages. Safe to run repeatedly."""
    count = 0
    cursor = 0
    while True:
        cursor, keys = await storage._client.scan(
            cursor=cursor,
            match="agentscope:user:*:session:*",
            count=100,
        )
        for key in keys:
            if key.endswith(":messages"):
                continue
            parts = key.split(":")
            if len(parts) != 5:
                continue
            user_id, session_id = parts[2], parts[4]
            if await _patent_agent(storage, user_id, session_id) is None:
                continue
            messages, _ = await storage.list_messages(user_id, session_id, limit=10_000)
            for message in messages:
                count += int(await archive_message(storage, user_id, session_id, message))
        if cursor == 0:
            break
    return count


@patent_observations_router.get("/agent-observations/{agent_id}")
async def get_agent_observations(
    agent_id: str,
    x_user_id: str = Header(..., alias="X-User-ID"),
    limit: int = Query(100, ge=1, le=500),
    include_records: bool = Query(False),
) -> dict:
    if _storage is None:
        raise HTTPException(503, "storage 未初始化")
    agent = await _storage.get_agent(x_user_id, agent_id)
    if agent is None:
        raise HTTPException(404, "未找到该智能体或无权查看")
    raw_records = await _storage._client.hvals(
        _KEY.format(user=x_user_id, agent=agent_id),
    )
    records = [json.loads(raw) for raw in raw_records]
    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    sessions = await _storage._client.scard(
        _SESSIONS_KEY.format(user=x_user_id, agent=agent_id),
    )
    tool_failures = 0
    for record in records:
        for block in record.get("content") or []:
            text = json.dumps(block, ensure_ascii=False)
            if block.get("type") == "tool_result" and any(
                marker in text for marker in ("异常", "失败", "error", "isError")
            ):
                tool_failures += 1
    return {
        "agent_id": agent_id,
        "sessions": sessions,
        "messages": len(records),
        "tool_failures": tool_failures,
        "records": _public_value(records[:limit]) if include_records else [],
    }
