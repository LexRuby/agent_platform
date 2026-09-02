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

import httpx
from pydantic import ConfigDict, Field, SecretStr

from agentscope.credential import CredentialBase

if TYPE_CHECKING:
    from agentscope.embedding import EmbeddingModelBase
    from agentscope.model import ChatModelBase

import logging

_logger = logging.getLogger("agentforge.ark")

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


def _write_chat_cards(names: list[str]) -> None:
    """把对话模型卡目录整体重写为给定模型列表（清除已下架的旧卡）。"""
    import yaml

    chat_dir = _ARK_MODELS_DIR / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    for old in chat_dir.glob("*.yaml"):
        old.unlink()
    for name in names:
        # 已知模型用人工核对过的规格，未知新模型用保守默认值
        label, ctx, out = _CHAT_MODELS.get(name, (name, 131_072, 16_384))
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


def _write_model_cards() -> None:
    """初始化模型卡目录（静态兜底；心跳同步成功后被真实列表覆盖）。"""
    import yaml

    _write_chat_cards(list(_CHAT_MODELS))

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


# ---------------- 模型列表心跳同步 ----------------

ARK_MODELS_URL = "https://ark.cn-beijing.volces.com/api/v3/models"
SYNC_INTERVAL_SECONDS = 24 * 3600

# 对话模型家族前缀（ARK 同时提供图像/视频/3D/向量等模型，需过滤）
_CHAT_FAMILIES = ("doubao-", "deepseek", "kimi", "glm", "qwen", "mistral")
# 对话家族里仍非对话的关键词（向量/视频/图像/语音/旧浏览版/预训练等）
_CHAT_EXCLUDE = (
    "embedding", "seedance", "seedream", "seededit", "seed3d", "seaweed",
    "wan2", "-i2v", "-t2v", "-i2i", "-flf2v", "tts", "audio", "browsing",
    "ui-tars", "pretrain", "smart-router",
)


def _is_chat_model(model_id: str) -> bool:
    return model_id.startswith(_CHAT_FAMILIES) and not any(
        k in model_id for k in _CHAT_EXCLUDE
    )


async def sync_ark_models(api_key: str | None = None) -> int:
    """从 ARK 拉取真实可用模型列表并刷新模型卡。

    Returns:
        同步到的对话模型数量；0 表示未同步（无 key 或拉取失败，保留现有卡片）。
    """
    import os

    key = api_key or os.environ.get("ARK_API_KEY", "")
    if not key:
        return 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            resp = await c.get(
                ARK_MODELS_URL, headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            ids = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:  # noqa: BLE001 - 心跳失败不应影响服务
        _logger.warning("ARK 模型同步失败（保留现有模型卡）: %s", e)
        return 0
    names = [i for i in ids if _is_chat_model(i)]
    _write_chat_cards(names)
    _logger.info("ARK 模型同步完成：%d 个对话模型", len(names))
    return len(names)


async def _heartbeat_loop() -> None:
    """启动即同步一次，此后每 24 小时刷新。"""
    import asyncio

    while True:
        await sync_ark_models()
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


def start_heartbeat() -> None:
    """在事件循环内启动心跳任务（服务 startup 时调用一次）。"""
    import asyncio

    asyncio.create_task(_heartbeat_loop())


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
