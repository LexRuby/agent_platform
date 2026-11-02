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

from .smart_writing import RequestLimitError, call_backend, load_tools

PATENT_AGENT_PROFILE_MARKER = "agentforge-profile: patent-drafting"

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
                if self._backend_unavailable:
                    payload = self._fallback_details(
                        kwargs,
                        "本会话此前已确认专利详情库不可用，已停止继续请求 30020",
                    )
                else:
                    try:
                        payload = await call_backend(self._spec, kwargs)
                    except RequestLimitError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 熔断远端详情服务
                        self._backend_unavailable = True
                        payload = self._fallback_details(
                            kwargs,
                            f"专利详情服务调用失败：{exc}",
                        )
                data = payload.get("data") if isinstance(payload, dict) else None
                err = data.get("err_msg") if isinstance(data, dict) else None
                if err:
                    self._backend_unavailable = True
                    payload = self._fallback_details(
                        kwargs,
                        f"专利详情库返回“{err}”",
                    )
        else:
            payload = await call_backend(self._spec, kwargs)
        text = json.dumps(payload, ensure_ascii=False)
        return ToolChunk(content=[TextBlock(text=text)])

    def _fallback_details(self, kwargs: dict, reason: str) -> dict:
        requested = [str(item).upper() for item in kwargs.get("pubids", [])]
        fallback = []
        for pubid in requested:
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
    if PATENT_AGENT_PROFILE_MARKER not in (prompt or ""):
        return []
    specs = _tool_specs()
    evidence_cache: dict[str, dict] = {}
    from .patent_quality_tools import patent_quality_tools

    return [
        *(
            PatentRegistrySearchTool(specs[name], evidence_cache)
            for name in _DEFAULT_TOOL_NAMES
        ),
        *patent_quality_tools(),
    ]
