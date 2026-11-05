"""专利写作智能体的默认检索与质量检查工具。

AgentScope 的 MCP 资源按会话工作区装配，无法保证新会话默认携带。
本模块复用现有 ``mcp_registry/patent_search.json`` 的工具定义和后端
路由，把专利/期刊检索作为标记过的专利写作智能体的原生只读工具。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk

from .smart_writing import (
    RequestLimitError,
    call_backend,
    load_tools,
    normalize_backend_arguments,
)

PATENT_AGENT_PROFILE_MARKER = "agentforge-profile: patent-drafting"
PATENT_EXTRACTION_PROFILE_MARKER = "agentforge-profile: patent-technology-extraction"
PATENT_ANALYSIS_PROFILE_MARKER = "agentforge-profile: patent-technology-analysis"

_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "mcp_registry"
    / "patent_search.json"
)
_DEFAULT_TOOL_NAMES = (
    "search_patents",
    "get_patent_details",
    "search_journals",
)


def _tool_specs() -> dict[str, dict]:
    return {tool["name"]: tool for tool in load_tools(_REGISTRY_PATH)}


class PatentRegistrySearchTool(ToolBase):
    """把现有专利检索注册包中的一个工具装配为 AgentScope 工具。"""

    is_concurrency_safe = True
    is_read_only = True

    def __init__(self, spec: dict, evidence_cache: dict[str, dict]) -> None:
        super().__init__()
        self._spec = spec
        self._evidence_cache = evidence_cache
        self._backend_unavailable = False
        self._detail_lock = asyncio.Lock()
        self._detail_cache: dict[tuple[str, str], dict] = {}
        self.name = spec["name"]
        self.description = spec["description"]
        self.input_schema = spec["inputSchema"]

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read-only patent prior-art search.",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        if self.name == "search_patents":
            payload = await call_backend(self._spec, kwargs)
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                for patent in data:
                    if not isinstance(patent, dict):
                        continue
                    pubid = str(patent.get("pubid") or "")
                    if pubid:
                        self._evidence_cache[pubid.upper()] = patent
        elif self.name == "get_patent_details":
            async with self._detail_lock:
                normalized = normalize_backend_arguments(self.name, kwargs)
                requested_fields = set(normalized["data_source"])
                source = str(normalized.get("patent_source") or "")
                requested_ids = [str(item) for item in normalized["pubids"]]
                cached, missing = self._split_cached_details(
                    source,
                    requested_ids,
                    requested_fields,
                )
                if self._backend_unavailable:
                    payload = self._fallback_details(
                        normalized,
                        "本会话此前已确认专利详情库不可用，已停止继续请求 30020",
                    )
                elif not missing:
                    payload = {
                        "data": {"patents": self._ordered_patents(requested_ids, cached)},
                        "cache_hit": True,
                    }
                else:
                    try:
                        remote_args = dict(normalized)
                        remote_args["pubids"] = missing
                        payload = await call_backend(self._spec, remote_args)
                    except RequestLimitError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 熔断远端详情服务
                        self._backend_unavailable = True
                        payload = self._fallback_details(
                            normalized,
                            f"专利详情服务调用失败：{exc}",
                        )
                data = payload.get("data") if isinstance(payload, dict) else None
                err = data.get("err_msg") if isinstance(data, dict) else None
                if err:
                    self._backend_unavailable = True
                    payload = self._fallback_details(
                        normalized,
                        f"专利详情库返回“{err}”",
                    )
                elif missing:
                    fresh = self._cache_detail_payload(source, payload)
                    combined = {**cached, **fresh}
                    if isinstance(data, dict) and isinstance(data.get("patents"), list):
                        payload = dict(payload)
                        payload["data"] = dict(data)
                        payload["data"]["patents"] = self._ordered_patents(
                            requested_ids,
                            combined,
                        )
        else:
            payload = await call_backend(self._spec, kwargs)
        text = json.dumps(payload, ensure_ascii=False)
        return ToolChunk(content=[TextBlock(text=text)])

    def _split_cached_details(
        self,
        source: str,
        pubids: list[str],
        requested_fields: set[str],
    ) -> tuple[dict[str, dict], list[str]]:
        cached: dict[str, dict] = {}
        missing: list[str] = []
        for pubid in pubids:
            patent = self._detail_cache.get((source, pubid.upper()))
            if patent is not None and requested_fields <= patent.keys():
                cached[pubid.upper()] = patent
            else:
                missing.append(pubid)
        return cached, missing

    def _cache_detail_payload(self, source: str, payload: dict) -> dict[str, dict]:
        data = payload.get("data") if isinstance(payload, dict) else None
        patents = data.get("patents") if isinstance(data, dict) else None
        cached: dict[str, dict] = {}
        if not isinstance(patents, list):
            return cached
        for patent in patents:
            if not isinstance(patent, dict):
                continue
            pubid = str(patent.get("pubid") or "").upper()
            if not pubid:
                continue
            key = (source, pubid)
            merged = {**self._detail_cache.get(key, {}), **patent}
            self._detail_cache[key] = merged
            cached[pubid] = merged
        return cached

    @staticmethod
    def _ordered_patents(pubids: list[str], patents: dict[str, dict]) -> list[dict]:
        return [patents[item.upper()] for item in pubids if item.upper() in patents]

    def _fallback_details(self, kwargs: dict, reason: str) -> dict:
        requested = [str(item).upper() for item in kwargs.get("pubids", [])]
        fallback = []
        for pubid in requested:
            detail = self._detail_cache.get(
                (str(kwargs.get("patent_source") or ""), pubid),
            )
            if detail is not None:
                fallback.append(detail)
                continue
            candidates = (pubid, pubid.removesuffix("A"), f"{pubid}A")
            cached = next(
                (
                    self._evidence_cache[key]
                    for key in candidates
                    if key in self._evidence_cache
                ),
                None,
            )
            if cached is not None:
                fallback.append(cached)
        return {
            "data": {"patents": fallback},
            "warning": (
                f"{reason}。已降级使用本轮 search_patents 返回的摘要、"
                "权利要求和说明书字段；这些材料只能作为检索初筛证据。"
            ),
            "evidence_level": "search_result_fallback",
            "circuit_open": self._backend_unavailable,
        }


async def patent_search_tools_for_agent(
    storage: Any,
    user_id: str,
    agent_id: str,
) -> list[ToolBase]:
    """带专利 profile 标识的智能体，每个会话自动获得基础工具。"""
    try:
        record = await storage.get_agent(user_id, agent_id)
        prompt = getattr(getattr(record, "data", None), "system_prompt", "")
    except Exception:  # noqa: BLE001 - 工具装配失败不应阻断整个会话
        return []
    prompt = prompt or ""
    if PATENT_EXTRACTION_PROFILE_MARKER in prompt:
        # 提取小A只处理用户提交的批次，避免无关检索拖慢流式输出。
        return []
    if not any(marker in prompt for marker in (
        PATENT_AGENT_PROFILE_MARKER,
        PATENT_ANALYSIS_PROFILE_MARKER,
    )):
        return []
    specs = _tool_specs()
    evidence_cache: dict[str, dict] = {}
    from .patent_quality_tools import patent_quality_tools

    tools = [
        *(
            PatentRegistrySearchTool(specs[name], evidence_cache)
            for name in _DEFAULT_TOOL_NAMES
        ),
        *patent_quality_tools(),
    ]
    if PATENT_ANALYSIS_PROFILE_MARKER in prompt:
        from .anysearch_tools import AnySearchTool

        tools.append(AnySearchTool())
    return tools
