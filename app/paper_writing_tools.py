"""Platform-managed tools for paper-drafting agents."""

from typing import Any

from agentscope.tool import ToolBase

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
_PAPER_TOOL_NAMES = ("search_journals",)


async def paper_writing_tools_for_agent(
    storage: Any,
    user_id: str,
    agent_id: str,
) -> list[ToolBase]:
    """Attach bounded journal search to marked paper-writing agents."""
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
        PatentRegistrySearchTool(specs[name], evidence_cache)
        for name in _PAPER_TOOL_NAMES
    ]
