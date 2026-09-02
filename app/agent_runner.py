from agentscope.message import UserMsg

from app.agent_factory import build_agent
from app.settings import Settings


def make_agent_runner(settings: Settings):
    """每个自动环节一个独立智能体执行：环节指令即任务，上游产出注入上下文。
    AGENTFORGE_FAKE_LLM=1 时跳过大模型，用于无 Key 的本地 E2E 演示。"""

    async def run_auto_step(task, step, context: str) -> str:
        if settings.fake_llm:
            return (
                f"[demo] 环节「{step.name}」完成（fake-llm 模式，未调用大模型与工具）。"
                f"输入：上游上下文 {len(context)} 字符，指令 {len(step.instruction)} 字符。"
            )
        agent = build_agent(settings)
        prompt = step.instruction
        if context:
            prompt = f"以下是前置环节的产出，作为本次任务的输入：\n\n{context}\n\n本次任务：\n{step.instruction}"
        result = await agent.reply(UserMsg(name="system", content=prompt))
        return result.get_text_content()

    return run_auto_step
