"""Platform-managed tools for paper-drafting agents."""

import asyncio
import json
import re
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolBase
from agentscope.tool import ToolChunk

from .anysearch_tools import ANYSEARCH_TOOL_NAME, AnySearchTool
from .patent_search_tools import PatentRegistrySearchTool, _tool_specs


PAPER_AGENT_PROFILE_MARKER = "agentforge-profile: paper-drafting"
PAPER_TEMPLATE_NAMES = (
    "论文研究现状智能体",
    "论文科学问题智能体",
    "论文方法论智能体",
    "论文实验与结果智能体",
    "论文讨论智能体",
    "论文结论智能体",
    "论文关键词智能体",
)
_PAPER_TOOL_NAMES = ("search_journals", ANYSEARCH_TOOL_NAME)
_JOURNAL_QUERY_FIELD = re.compile(r"(?:title|abst|keywords):[^\s()]+")


class BoundedJournalSearchTool(PatentRegistrySearchTool):
    """Journal search limited to two serialized attempts per chat session."""

    def __init__(self, spec: dict, evidence_cache: dict[str, dict]) -> None:
        super().__init__(spec, evidence_cache)
        self._journal_lock = asyncio.Lock()
        self._journal_calls = 0
        self._journal_circuit_open = False
        self._journal_cache: dict[str, dict] = {}

    @staticmethod
    def _chunk(payload: dict) -> ToolChunk:
        return ToolChunk(content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))])

    async def call(self, **kwargs: Any) -> ToolChunk:
        normalized = dict(kwargs)
        query = " ".join(str(normalized.get("query") or "").split())
        if not query or not _JOURNAL_QUERY_FIELD.search(query):
            return self._chunk({
                "error": (
                    "search_journals 只接受中文布尔检索式，必须包含 "
                    "title:/abst:/keywords: 字段，例如 keywords:联邦学习 AND keywords:隐私保护"
                ),
                "request_sent": False,
            })
        try:
            requested_size = int(normalized.get("size", 20))
        except (TypeError, ValueError):
            return self._chunk({"error": "search_journals 的 size 必须是整数", "request_sent": False})
        normalized["query"] = query
        normalized["size"] = max(1, min(requested_size, 20))
        cache_key = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        async with self._journal_lock:
            if cache_key in self._journal_cache:
                return self._chunk({**self._journal_cache[cache_key], "cache_hit": True})
            if self._journal_circuit_open:
                return self._chunk({
                    "error": "期刊检索本会话此前已失败，已停止重复请求；请改用 AnySearch 补充核验",
                    "circuit_open": True,
                    "request_sent": False,
                })
            if self._journal_calls >= 2:
                return self._chunk({
                    "error": "期刊检索本会话最多调用 2 次，请基于已有结果完成任务",
                    "budget_exhausted": True,
                    "request_sent": False,
                })
            self._journal_calls += 1
            try:
                chunk = await super().call(**normalized)
            except Exception as exc:  # noqa: BLE001 - fail closed for this session
                self._journal_circuit_open = True
                return self._chunk({
                    "error": str(exc),
                    "circuit_open": True,
                    "request_sent": True,
                })
            payload = json.loads(chunk.content[0].text)
            self._journal_cache[cache_key] = payload
            return chunk


async def paper_writing_tools_for_agent(
    storage: Any,
    user_id: str,
    agent_id: str,
) -> list[ToolBase]:
    """Attach bounded journal and web search to marked paper-writing agents."""
    try:
        record = await storage.get_agent(user_id, agent_id)
        prompt = getattr(getattr(record, "data", None), "system_prompt", "")
    except Exception:  # noqa: BLE001 - tool assembly must not block chat
        return []
    if PAPER_AGENT_PROFILE_MARKER not in (prompt or ""):
        return []
    specs = _tool_specs()
    evidence_cache: dict[str, dict] = {}
    return [
        BoundedJournalSearchTool(specs["search_journals"], evidence_cache),
        AnySearchTool(),
    ]
