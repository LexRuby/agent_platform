"""Runtime capability inventory for an agent.

The agent editor only stores prompts.  Most tools are attached later by the
workspace and ``extra_agent_tools`` hooks, so users need a separate endpoint
that describes the effective, platform-managed capability set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .patent_search_tools import (
    PATENT_AGENT_PROFILE_MARKER,
    _DEFAULT_TOOL_NAMES,
    _tool_specs,
)
from .patent_quality_tools import patent_quality_tools
from .anysearch_tools import ANYSEARCH_DESCRIPTION, ANYSEARCH_TOOL_NAME
from .paper_writing_tools import PAPER_AGENT_PROFILE_MARKER


agent_capabilities_router = APIRouter(tags=["agent-capabilities"])
_storage: Any = None


def init_agent_capabilities(storage: Any) -> None:
    global _storage
    _storage = storage


class CapabilityTool(BaseModel):
    name: str
    description: str
    source: str


class CapabilityGroup(BaseModel):
    id: str
    name: str
    tools: list[CapabilityTool]


class AgentCapabilitiesResponse(BaseModel):
    agent_id: str
    agent_name: str
    groups: list[CapabilityGroup]
    web_search_enabled: bool
    notes: list[str]


_WORKSPACE_TOOLS = [
    ("Read", "读取当前对话或项目空间内的文件"),
    ("Write", "在当前空间创建或覆盖文件"),
    ("Edit", "精确修改当前空间内的文件"),
    ("Glob", "按文件名模式查找当前空间内的文件"),
    ("Grep", "在当前空间内检索文件内容"),
    ("Bash", "在当前空间内执行受边界约束的命令"),
]

_PLANNING_TOOLS = [
    ("TaskCreate", "创建任务项"),
    ("TaskList", "查看任务进度"),
    ("TaskGet", "查看任务详情"),
    ("TaskUpdate", "更新任务状态与内容"),
]


def _group(group_id: str, name: str, source: str, tools: list[tuple[str, str]]) -> CapabilityGroup:
    return CapabilityGroup(
        id=group_id,
        name=name,
        tools=[
            CapabilityTool(name=tool_name, description=description, source=source)
            for tool_name, description in tools
        ],
    )


@agent_capabilities_router.get(
    "/agent-capabilities/{agent_id}",
    response_model=AgentCapabilitiesResponse,
)
async def get_agent_capabilities(
    agent_id: str,
    x_user_id: str = Header(..., alias="X-User-ID"),
) -> AgentCapabilitiesResponse:
    if _storage is None:
        raise HTTPException(503, "storage 未初始化")
    agent = await _storage.get_agent(x_user_id, agent_id)
    if agent is None:
        raise HTTPException(404, "未找到该智能体或无权查看")

    groups = [
        _group("workspace", "工作空间", "平台内置", _WORKSPACE_TOOLS),
        _group("planning", "任务管理", "平台内置", _PLANNING_TOOLS),
        _group(
            "skills",
            "技能沉淀",
            "平台扩展",
            [("SolidifySkill", "把验证有效的方法固化为可复用技能")],
        ),
    ]
    prompt = agent.data.system_prompt or ""
    is_patent = PATENT_AGENT_PROFILE_MARKER in prompt
    is_paper = PAPER_AGENT_PROFILE_MARKER in prompt
    if is_patent:
        specs = _tool_specs()
        groups.append(
            _group(
                "patent-search",
                "专利与期刊检索",
                "专利写作配置",
                [(name, specs[name]["description"]) for name in _DEFAULT_TOOL_NAMES],
            ),
        )
        groups.append(
            _group(
                "patent-quality",
                "专利质量检查",
                "专利写作配置",
                [(tool.name, tool.description) for tool in patent_quality_tools()],
            ),
        )

    if is_paper:
        specs = _tool_specs()
        paper_tools = [
            ("search_journals", specs["search_journals"]["description"]),
            (ANYSEARCH_TOOL_NAME, ANYSEARCH_DESCRIPTION),
        ]
        groups.append(
            _group(
                "paper-search",
                "学术与联网检索",
                "论文写作配置",
                paper_tools,
            ),
        )

    notes = ["会话中临时安装的 MCP 和技能会在对应会话内追加。"]
    if is_patent:
        notes.append("当前专利写作配置没有通用互联网搜索工具。")
    if is_paper:
        notes.append("论文写作配置优先使用期刊检索，并用 AnySearch 补充和交叉核验。")
    return AgentCapabilitiesResponse(
        agent_id=agent_id,
        agent_name=agent.data.name,
        groups=groups,
        web_search_enabled=is_paper,
        notes=notes,
    )
