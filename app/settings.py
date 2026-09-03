import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

ARK_BASE_URL_DEFAULT = "https://ark.cn-beijing.volces.com/api/v3"


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    model_name: str
    dashscope_api_key: str
    ark_api_key: str
    ark_base_url: str
    midplatform_base_url: str
    midplatform_token: str
    studio_url: str
    fake_llm: bool


def load_settings() -> Settings:
    return Settings(
        llm_provider=os.environ.get("AGENTFORGE_PROVIDER", "dashscope"),
        model_name=os.environ.get("AGENTFORGE_MODEL", "qwen-max"),
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        ark_api_key=os.environ.get("ARK_API_KEY", ""),
        ark_base_url=os.environ.get("ARK_BASE_URL", ARK_BASE_URL_DEFAULT),
        midplatform_base_url=os.environ.get("MIDPLATFORM_BASE_URL", "http://127.0.0.1:9000"),
        midplatform_token=os.environ.get("MIDPLATFORM_TOKEN", ""),
        studio_url=os.environ.get("AGENTFORGE_STUDIO_URL", ""),
        # 无 API Key 的本地 E2E 演示模式：跳过大模型调用
        fake_llm=os.environ.get("AGENTFORGE_FAKE_LLM", "") == "1",
    )
