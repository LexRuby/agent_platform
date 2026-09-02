from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential, OpenAICredential
from agentscope.middleware import TracingMiddleware
from agentscope.model import ChatModelBase, DashScopeChatModel, OpenAIChatModel
from agentscope.tool import Toolkit

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
        toolkit=Toolkit(
            tools=[
                SearchAdmissionData(),
                SearchKnowledge(),
                WritingGenerate(),
                WritingPolish(),
            ],
        ),
    )
