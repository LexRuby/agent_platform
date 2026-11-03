"""Bounded AnySearch web search for writing agents."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk


ANYSEARCH_API_URL = "https://api.anysearch.com/v1/search"
ANYSEARCH_TOOL_NAME = "anysearch_search"
ANYSEARCH_DESCRIPTION = (
    "联网搜索公开网页，用于补充期刊库未覆盖的最新资料、项目页面和术语证据。"
    "返回标题、网址和摘要；网页结果不能替代正式论文元数据或证明学术结论。"
)
_MAX_RESULTS = 10
_MAX_CALLS_PER_SESSION = 2
_TIMEOUT = 30.0


def _api_url() -> str:
    return os.environ.get("ANYSEARCH_BASE_URL", ANYSEARCH_API_URL).rstrip("/")


def _parse_response(body: Any, max_results: int) -> list[dict[str, str]]:
    if not isinstance(body, dict) or body.get("code") != 0:
        raise RuntimeError("AnySearch 返回了无效的响应状态")
    data = body.get("data")
    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        raise RuntimeError("AnySearch 响应缺少 data.results")
    results: list[dict[str, str]] = []
    for index, item in enumerate(raw_results[:max_results]):
        if not isinstance(item, dict):
            raise RuntimeError(f"AnySearch 第 {index + 1} 条结果格式无效")
        title, url, snippet = item.get("title"), item.get("url"), item.get("snippet")
        if not all(isinstance(value, str) for value in (title, url, snippet)) or not url:
            raise RuntimeError(f"AnySearch 第 {index + 1} 条结果字段不完整")
        results.append({
            "title": title[:500],
            "url": url[:2000],
            "snippet": snippet[:3000],
        })
    return results


async def search_anysearch(
    query: str,
    max_results: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("ANYSEARCH_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await active_client.post(
            _api_url(),
            headers=headers,
            json={"query": query, "max_results": max_results},
        )
        if response.status_code in {401, 403} and not api_key:
            raise RuntimeError("AnySearch 服务要求 API Key，但服务器尚未配置 ANYSEARCH_API_KEY")
        response.raise_for_status()
        results = _parse_response(response.json(), max_results)
        return {"provider": "AnySearch", "query": query, "results": results}
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"AnySearch 请求失败（HTTP {exc.response.status_code}）") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"AnySearch 请求失败：{exc}") from exc
    finally:
        if owns_client:
            await active_client.aclose()


class AnySearchTool(ToolBase):
    """Session-scoped web search with caching, a budget, and failure circuit."""

    is_concurrency_safe = True
    is_read_only = True
    name = ANYSEARCH_TOOL_NAME
    description = ANYSEARCH_DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "具体、可检索的查询，建议包含中英文核心术语",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "default": 8,
                "description": "返回结果数，默认 8，最多 10",
            },
        },
        "required": ["query"],
    }

    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()
        self._calls = 0
        self._circuit_open = False
        self._cache: dict[tuple[str, int], dict[str, Any]] = {}

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read-only public web search.",
        )

    @staticmethod
    def _chunk(payload: dict[str, Any]) -> ToolChunk:
        return ToolChunk(content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))])

    async def call(self, **kwargs: Any) -> ToolChunk:
        query = " ".join(str(kwargs.get("query") or "").split())
        if not query:
            return self._chunk({"error": "query 不能为空", "request_sent": False})
        if len(query) > 500:
            return self._chunk({"error": "query 最多 500 个字符", "request_sent": False})
        requested = kwargs.get("max_results", 8)
        try:
            max_results = max(1, min(int(requested), _MAX_RESULTS))
        except (TypeError, ValueError):
            return self._chunk({"error": "max_results 必须是整数", "request_sent": False})
        cache_key = (query.casefold(), max_results)

        async with self._lock:
            if cache_key in self._cache:
                return self._chunk({**self._cache[cache_key], "cache_hit": True})
            if self._circuit_open:
                return self._chunk({
                    "error": "AnySearch 本会话此前调用失败，已停止重复请求",
                    "circuit_open": True,
                    "request_sent": False,
                })
            if self._calls >= _MAX_CALLS_PER_SESSION:
                return self._chunk({
                    "error": "AnySearch 本会话最多调用 2 次，请基于已有结果完成任务",
                    "budget_exhausted": True,
                    "request_sent": False,
                })
            self._calls += 1
            try:
                payload = await search_anysearch(query, max_results)
            except Exception as exc:  # noqa: BLE001 - return a useful tool result
                self._circuit_open = True
                return self._chunk({
                    "error": str(exc),
                    "circuit_open": True,
                    "request_sent": True,
                })
            self._cache[cache_key] = payload
            return self._chunk(payload)
