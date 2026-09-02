"""豆包 ARK 凭据类型：让官方 Web UI 的模型下拉显示 ARK 真实模型。

背景：用 openai_credential 接 ARK 时，模型下拉是 OpenAI 的静态目录
（GPT-4o 等），与 ARK 账号实际可用模型（doubao/deepseek/kimi/glm 等 130 个）
不符，选了也无法调用。本模块注册一个独立的 ``ark_credential`` 凭据类型：

- 协议复用 OpenAI 兼容实现（ARK 本身就是 OpenAI-compatible）
- 模型卡改为 ARK 目录（app/ark_models/chat、app/ark_models/embedding），
  覆盖常用 doubao-seed / deepseek / kimi / glm 对话模型与 embedding 模型
- 在凭据页新建时选「豆包 ARK」类型，填 ARK key 即可（base_url 已内置默认值）
"""

from pathlib import Path
from typing import Literal, Type, TYPE_CHECKING

from pydantic import ConfigDict, Field, SecretStr

from agentscope.credential import CredentialBase

if TYPE_CHECKING:
    from agentscope.embedding import EmbeddingModelBase
    from agentscope.model import ChatModelBase

_ARK_MODELS_DIR = Path(__file__).parent / "ark_models"

# 常用 ARK 对话模型卡（模型名与 ARK 平台一致，详见 ark_models/chat/*.yaml）
_CHAT_MODELS = {
    "doubao-seed-2-1-pro-260628": ("Doubao Seed 2.1 Pro", 256_000, 32_768),
    "doubao-seed-2-1-turbo-260628": ("Doubao Seed 2.1 Turbo", 256_000, 32_768),
    "doubao-seed-2-0-pro-260215": ("Doubao Seed 2.0 Pro", 256_000, 32_768),
    "doubao-seed-2-0-mini-260215": ("Doubao Seed 2.0 Mini", 256_000, 16_384),
    "doubao-seed-2-0-lite-260215": ("Doubao Seed 2.0 Lite", 256_000, 16_384),
    "doubao-seed-1-6-250615": ("Doubao Seed 1.6", 256_000, 16_384),
    "doubao-seed-1-6-flash-250615": ("Doubao Seed 1.6 Flash", 256_000, 16_384),
    "doubao-seed-1-6-thinking-250615": ("Doubao Seed 1.6 Thinking", 256_000, 16_384),
    "doubao-seed-character-260628": ("Doubao Seed Character", 256_000, 16_384),
    "deepseek-v4-pro-260425": ("DeepSeek V4 Pro", 131_072, 16_384),
    "deepseek-v4-flash-260425": ("DeepSeek V4 Flash", 131_072, 16_384),
    "kimi-k2-thinking-251104": ("Kimi K2 Thinking", 262_144, 16_384),
    "glm-5-2-260617": ("GLM 5.2", 131_072, 16_384),
}

# ARK embedding 模型卡
_EMBEDDING_MODELS = {
    "doubao-embedding-large-text-250515": ("Doubao Embedding Large Text", 4096, 2560),
    "doubao-embedding-vision-251215": ("Doubao Embedding Vision", 8192, 3072),
}


def _write_model_cards() -> None:
    """把模型卡目录生成为 YAML（幂等，服务启动时执行一次）。"""
    import yaml

    chat_dir = _ARK_MODELS_DIR / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    for name, (label, ctx, out) in _CHAT_MODELS.items():
        card = {
            "name": name,
            "label": label,
            "status": "active",
            "input_types": ["text/plain"],
            "output_types": ["text/plain"],
            "context_size": ctx,
            "output_size": out,
            "parameter_overrides": {
                # OpenAI 专属参数在 ARK 上无意义，前端隐藏
                "voice": {"hidden": True},
                "reasoning_effort": {"hidden": True},
            },
        }
        (chat_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(card, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    emb_dir = _ARK_MODELS_DIR / "embedding"
    emb_dir.mkdir(parents=True, exist_ok=True)
    for name, (label, ctx, dim) in _EMBEDDING_MODELS.items():
        card = {
            "name": name,
            "label": label,
            "status": "active",
            "input_types": ["text/plain"],
            "output_types": ["application/x-embedding"],
            "context_size": ctx,
            "dimensions": dim,
        }
        (emb_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(card, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


_write_model_cards()


from agentscope.model import OpenAIChatModel  # noqa: E402


class ArkChatModel(OpenAIChatModel):
    """OpenAI 兼容协议 + ARK 模型目录。"""

    @classmethod
    def list_models(cls, custom_yaml_dir: str | None = None) -> list:
        return super().list_models(
            custom_yaml_dir=str(_ARK_MODELS_DIR / "chat"),
        )


from agentscope.embedding import OpenAIEmbeddingModel  # noqa: E402


class ArkEmbeddingModel(OpenAIEmbeddingModel):
    """OpenAI 兼容协议 + ARK embedding 目录。"""

    @classmethod
    def list_models(cls, custom_yaml_dir: str | None = None) -> list:
        return super().list_models(
            custom_yaml_dir=str(_ARK_MODELS_DIR / "embedding"),
        )


class ArkCredential(CredentialBase):
    """豆包 ARK（火山方舟）凭据：OpenAI 兼容协议。"""

    model_config = ConfigDict(
        title="豆包 ARK",
    )

    type: Literal["ark_credential"] = "ark_credential"
    """The credential type."""

    api_key: SecretStr = Field(
        description="ARK API Key（火山方舟控制台获取）",
    )
    """The API key."""

    organization: str | None = Field(
        default=None,
        description="OpenAI 兼容字段，ARK 无需填写",
    )
    """Unused for ARK; kept for OpenAIChatModel compatibility."""

    base_url: str | None = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        description="ARK API 地址（OpenAI 兼容）",
    )
    """The ARK API base URL."""

    @classmethod
    def get_chat_model_class(cls) -> Type["ChatModelBase"]:
        return ArkChatModel

    @classmethod
    def get_embedding_model_class(cls) -> Type["EmbeddingModelBase"]:
        return ArkEmbeddingModel

    @classmethod
    def get_tts_model_classes(cls) -> list:
        # ARK 的 TTS 走独立接口（非 OpenAI 兼容），暂不提供
        return []
