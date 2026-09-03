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


def _write_chat_cards(cards: list[dict]) -> None:
    """把对话模型卡目录整体重写为给定卡片列表（清除已下架的旧卡）。

    文件名带 4 位序号前缀固化列表顺序（发布日期降序）：
    agentscope 的 list_models 按 glob 目录序读取（不可靠），
    ArkChatModel.list_models 再按文件名字典序重排即恢复发布序。
    """
    import yaml

    chat_dir = _ARK_MODELS_DIR / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    for old in chat_dir.glob("*.yaml"):
        old.unlink()
    for i, card in enumerate(cards):
        (chat_dir / f"{i:04d}-{card['name']}.yaml").write_text(
            yaml.safe_dump(card, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _static_cards() -> list[dict]:
    """从内置核对表构造静态兜底卡片（心跳同步失败时使用）。"""
    return [
        {
            "name": mid,
            "label": label + _price_tag(mid.rsplit("-", 1)[0]),
            "status": "active",
            "input_types": ["text/plain"],
            "output_types": ["text/plain"],
            "context_size": ctx,
            "output_size": out,
            "parameters_overrides": {
                "voice": {"hidden": True},
                "reasoning_effort": {"hidden": True},
            },
        }
        for mid, (label, ctx, out) in _CHAT_MODELS.items()
    ]


def _write_model_cards() -> None:
    """初始化模型卡目录（静态兜底；心跳同步成功后被真实列表覆盖）。"""
    import yaml

    _write_chat_cards(_static_cards())

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

# 下架/退役状态（status 字段缺失 = 正常可用）
_UNUSABLE_STATUS = ("Shutdown", "Retiring")

# 对话模型家族前缀（task_type 缺失时的兜底判定）
_CHAT_FAMILIES = ("doubao-", "deepseek", "kimi", "glm", "qwen", "mistral")
# 对话家族里仍非对话的关键词（向量/视频/图像/语音/旧浏览版/预训练等）
_CHAT_EXCLUDE = (
    "embedding", "seedance", "seedream", "seededit", "seed3d", "seaweed",
    "wan2", "-i2v", "-t2v", "-i2i", "-flf2v", "tts", "audio", "browsing",
    "ui-tars", "pretrain", "smart-router",
)

# 内置价格参考表（官方定价页数据，元/百万token，输入/输出取最低档；
# 分段计价模型为"起"价。key 为连字符形式的基础名。
# ARK API 不提供价格查询，此表需随官方调价更新）
_PRICE_TABLE = {
    "doubao-seed-2-1-pro": (6.0, 30.0),
    "doubao-seed-2-1-turbo": (3.0, 15.0),
    "doubao-seed-evolving": (6.0, 30.0),
    "doubao-seed-2-0-pro": (3.2, 16.0),
    "doubao-seed-2-0-code": (3.2, 16.0),
    "doubao-seed-2-0-lite": (0.6, 3.6),
    "doubao-seed-2-0-mini": (0.2, 2.0),
    "doubao-seed-1-8": (0.8, 2.0),
    "doubao-seed-1-6": (0.8, 2.0),
    "doubao-seed-1-6-lite": (0.3, 0.6),
    "doubao-seed-1-6-flash": (0.15, 1.5),
    "doubao-seed-code": (1.2, 8.0),
    "doubao-seed-character": (0.8, 2.0),
    "deepseek-v4-pro": (9.0, 27.0),
    "deepseek-v4-flash": (3.0, 9.0),
}

# 品牌名美化映射（基础名前缀 → 展示名）
_BRAND_PRETTY = {
    "doubao": "Doubao", "deepseek": "DeepSeek", "kimi": "Kimi",
    "glm": "GLM", "qwen": "Qwen", "mistral": "Mistral",
}


def _is_chat_model(m: dict) -> bool:
    """判定是否对话模型：优先 task_type，缺失时按家族前缀兜底。"""
    if "TextGeneration" in (m.get("task_type") or []):
        return True
    mid = m.get("id", "")
    return mid.startswith(_CHAT_FAMILIES) and not any(
        k in mid for k in _CHAT_EXCLUDE
    )


def _is_usable(m: dict) -> bool:
    """可用+开通：status 字段缺失视为正常，Shutdown/Retiring 为下架/退役。"""
    return m.get("status") not in _UNUSABLE_STATUS


# 缩写词映射（保持全大写，避免 "ga" 被写成 "Ga"）
_ACRONYMS = {"ga": "GA"}

def _pretty_label(base_name: str) -> str:
    """基础名 → 可读名：doubao-seed-2.1-pro → Doubao Seed 2.1 Pro。"""
    parts = base_name.split("-")
    out = [_BRAND_PRETTY.get(parts[0].lower(), parts[0].capitalize())]
    for w in parts[1:]:
        # 版本号等以数字开头的词保持原样（2.1、5.2）；缩写词全大写（GA）
        if w[:1].isdigit():
            out.append(w)
        else:
            out.append(_ACRONYMS.get(w.lower(), w.capitalize()))
    return " ".join(out)


def _price_tag(base_name: str) -> str:
    """价格标注（点号/连字符基础名均可匹配）；空串表示无内置价格。"""
    normalized = base_name.replace(".", "-")
    for name, (pin, pout) in _PRICE_TABLE.items():
        if normalized == name or normalized.startswith(name + "-"):
            return f" · ¥{pin:g}/¥{pout:g} 起每百万token"
    return ""


def _input_types(m: dict) -> list[str]:
    """按 modalities 映射 AgentScope 输入类型。"""
    mods = (m.get("modalities") or {}).get("input_modalities") or []
    types = ["text/plain"]
    if "image" in mods:
        types += ["image/jpeg", "image/png"]
    return types


def _chat_card(m: dict) -> dict:
    """从 /v3/models 的模型对象构建模型卡 dict。"""
    mid = m["id"]
    base = m.get("name") or mid
    tl = m.get("token_limits") or {}
    # 规格优先级：API 真实值 > 内置核对表 > 保守默认
    known = _CHAT_MODELS.get(mid, (base, 131_072, 16_384))
    ctx = tl.get("context_window") or known[1]
    out = tl.get("max_output_token_length") or known[2]
    return {
        "name": mid,
        "label": _pretty_label(base) + _price_tag(base),
        "status": "active",
        "input_types": _input_types(m),
        "output_types": ["text/plain"],
        "context_size": ctx,
        "output_size": out,
        "parameters_overrides": {
            # OpenAI 专属参数在 ARK 上无意义，前端隐藏
            "voice": {"hidden": True},
            "reasoning_effort": {"hidden": True},
        },
    }


async def sync_ark_models(api_key: str | None = None) -> int:
    """从 ARK 拉取真实可用模型列表并刷新模型卡。

    过滤：仅保留可用（未下架/未退役）的对话模型；
    排序：按发布时间（created）降序，最新的排最前。

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
            models = resp.json().get("data", [])
    except Exception as e:  # noqa: BLE001 - 心跳失败不应影响服务
        _logger.warning("ARK 模型同步失败（保留现有模型卡）: %s", e)
        return 0
    usable = [
        m for m in models
        if _is_usable(m) and _is_chat_model(m)
    ]
    usable.sort(key=lambda m: -m.get("created", 0))  # 最新发布在前
    _write_chat_cards([_chat_card(m) for m in usable])
    _logger.info("ARK 模型同步完成：%d 个可用对话模型", len(usable))
    return len(usable)


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


from agentscope.model import ModelCard, OpenAIChatModel  # noqa: E402


class ArkChatModel(OpenAIChatModel):
    """OpenAI 兼容协议 + ARK 模型目录。"""

    @classmethod
    def list_models(cls, custom_yaml_dir: str | None = None) -> list:
        chat_dir = _ARK_MODELS_DIR / "chat"
        if not chat_dir.exists():
            return []
        # 按文件名序号前缀（发布日期降序）稳定排序后加载
        cards = []
        for p in sorted(chat_dir.glob("*.yaml"), key=lambda x: x.name):
            try:
                cards.append(
                    ModelCard.from_yaml(
                        yaml_path=str(p), parameter_class=cls.Parameters,
                    ),
                )
            except Exception:  # noqa: BLE001 - 单卡损坏跳过不影响整体
                _logger.warning("ARK 模型卡加载失败（跳过）: %s", p.name)
        return cards


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
