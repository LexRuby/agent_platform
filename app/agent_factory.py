"""自定义 Agent 构建：按注册中心里的 Agent 定义（工具引用 + 系统提示词）组装 AgentScope Agent。

工具引用格式：
  builtin:<tool_name>          内置中台工具（app/tools）
  mcp:<server>:<tool_name>     已注册 MCP Server 提供的工具
"""

import json

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential, OpenAICredential
from agentscope.message import TextBlock
from agentscope.middleware import TracingMiddleware
from agentscope.model import ChatModelBase, DashScopeChatModel, OpenAIChatModel
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from app import mcp_client
from app.registry import Registry
from app.settings import ARK_BASE_URL_DEFAULT, Settings
from app.tools.search import SearchAdmissionData, SearchKnowledge
from app.tools.writing import WritingGenerate, WritingPolish
from app.tracing import setup_tracing

SYSTEM_PROMPT = (
    "你是智汇平台的任务执行智能体，负责完成单个自动化环节。"
    "严格按当前环节的指令执行：需要数据时先调用检索工具，"
    "需要产出报告时调用写作工具；工具返回的数据是唯一事实来源，"
    "不得编造数据。完成后输出该环节的最终结果。"
)

# 内置工具注册表（零代码控制台"基础工具"目录的来源）
BUILTIN_TOOLS: list[ToolBase] = [
    SearchAdmissionData(),
    SearchKnowledge(),
    WritingGenerate(),
    WritingPolish(),
]


def build_model(settings: Settings) -> ChatModelBase:
    if settings.llm_provider == "doubao":
        # 火山方舟（豆包），OpenAI 兼容协议
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=settings.ark_api_key,
                base_url=settings.ark_base_url or ARK_BASE_URL_DEFAULT,
            ),
            model=settings.model_name,
        )
    return DashScopeChatModel(
        credential=DashScopeCredential(api_key=settings.dashscope_api_key),
        model=settings.model_name,
    )


def build_agent(settings: Settings) -> Agent:
    middlewares = []
    if setup_tracing(settings.studio_url):
        middlewares.append(TracingMiddleware())
    return Agent(
        name="agentforge_worker",
        system_prompt=SYSTEM_PROMPT,
        model=build_model(settings),
        middlewares=middlewares,
        toolkit=Toolkit(tools=BUILTIN_TOOLS),
    )


class McpTool(ToolBase):
    """把 MCP Server 上的一个工具包装成 AgentScope 工具。"""

    is_concurrency_safe = True
    is_read_only = True

    def __init__(self, server_spec: dict, tool_spec: dict):
        self.server_spec = server_spec
        self.tool_spec = tool_spec
        # 避免跨 server 工具重名：mcp_<server>_<tool>
        self.name = f"mcp_{server_spec['name']}_{tool_spec['name']}"
        self.description = (
            f"[MCP:{server_spec['name']}] {tool_spec.get('description', '')}".strip()
        )
        self.input_schema = tool_spec.get("input_schema") or {
            "type": "object",
            "properties": {},
        }

    async def check_permissions(
        self, tool_input: dict, context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Registered MCP tool call.",
        )

    async def call(self, **kwargs) -> ToolChunk:
        text = await mcp_client.call_tool(
            self.server_spec["url"],
            self.tool_spec["name"],
            kwargs,
            token=self.server_spec.get("token") or None,
        )
        if not text:
            text = json.dumps({"result": "ok"}, ensure_ascii=False)
        return ToolChunk(content=[TextBlock(text=text[:8000])])


def _resolve_tools(registry: Registry, tool_refs: list[str]) -> list[ToolBase]:
    """把工具引用列表解析成工具实例集合。"""
    by_name = {t.name: t for t in BUILTIN_TOOLS}
    tools: list[ToolBase] = []
    for ref in tool_refs:
        kind, _, rest = ref.partition(":")
        if kind == "builtin":
            if rest in by_name:
                tools.append(by_name[rest])
        elif kind == "mcp":
            server_name, _, tool_name = rest.partition(":")
            server = registry.get_mcp_server(server_name)
            if not server:
                continue
            spec = next(
                (t for t in server.get("tools", []) if t["name"] == tool_name), None,
            )
            if spec:
                tools.append(McpTool(server, spec))
    return tools


def build_custom_agent(settings: Settings, registry: Registry, agent_def: dict) -> Agent:
    """按 Agent 定义组装一个可对话的 Agent（工作空间 / 对外服务共用）。"""
    middlewares = []
    if setup_tracing(settings.studio_url):
        middlewares.append(TracingMiddleware())
    return Agent(
        name=f"custom_{agent_def['id']}",
        system_prompt=agent_def["system_prompt"],
        model=build_model(settings),
        middlewares=middlewares,
        toolkit=Toolkit(tools=_resolve_tools(registry, agent_def.get("tools", []))),
    )
