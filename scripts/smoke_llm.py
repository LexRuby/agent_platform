"""LLM 直连冒烟：一次最小调用，验证 provider 配置 / key / 模型名。"""

from agentscope.message import UserMsg

from app.agent_factory import build_agent
from app.settings import load_settings


async def main() -> None:
    settings = load_settings()
    print(f"provider={settings.llm_provider} model={settings.model_name} key=***{settings.ark_api_key[-4:]}")
    agent = build_agent(settings)
    result = await agent.reply(UserMsg(name="user", content="只回复两个字：成功"))
    print("回复:", result.get_text_content())


import asyncio

asyncio.run(main())
