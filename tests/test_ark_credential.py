"""ark_credential 模块测试：过滤规则、模型卡写入、心跳同步（mock HTTP）、凭据 schema。

不访问真实 ARK：HTTP 层用假 AsyncClient 替换；模型卡目录隔离到 tmp_path。
"""

import types

import pytest
import yaml

from app import ark_credential as ark


@pytest.fixture
def cards_dir(tmp_path, monkeypatch):
    d = tmp_path / "ark_models"
    monkeypatch.setattr(ark, "_ARK_MODELS_DIR", d)
    return d


class TestChatModelFilter:
    """_is_chat_model：对话模型家族过滤规则。"""

    @pytest.mark.parametrize("model_id", [
        "doubao-seed-2-1-pro-260628",
        "doubao-seed-1-6-flash-250615",
        "doubao-seed-evolving",
        "deepseek-v3-250324",
        "deepseek-r1-250120",
        "kimi-k2-250711",
        "glm-4-5-air-20250728",
        "qwen3-32b-20250429",
        "mistral-7b-instruct-v0.2",
        # 翻译模型走 chat 接口，属对话类
        "doubao-seed-translation-250915",
    ])
    def test_chat_families_accepted(self, model_id):
        assert ark._is_chat_model(model_id) is True

    @pytest.mark.parametrize("model_id", [
        # 向量模型
        "doubao-embedding-large-text-250515",
        "doubao-embedding-vision-251215",
        # 视频/图像/3D/编辑
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedream-3-0-t2i-250415",
        "doubao-seededit-3-0-i2i-250628",
        "doubao-seed3d-1-0-250928",
        "doubao-seaweed-241128",
        "wan2-1-14b-i2v-250225",
        "wan2-1-14b-t2v-250225",
        "wan2-1-14b-flf2v-250417",
        # 旧浏览版 / 预训练 / 路由
        "doubao-pro-4k-browsing-240524",
        "doubao-lite-4k-pretrain-character-240516",
        "doubao-smart-router-250928",
        # UI 智能体
        "doubao-1-5-ui-tars-250328",
        # 完全无关家族
        "some-unknown-model",
    ])
    def test_non_chat_models_rejected(self, model_id):
        assert ark._is_chat_model(model_id) is False


class TestWriteChatCards:
    def test_known_model_uses_curated_spec(self, cards_dir):
        ark._write_chat_cards(["doubao-seed-2-1-pro-260628"])
        card = yaml.safe_load(
            (cards_dir / "chat" / "doubao-seed-2-1-pro-260628.yaml").read_text(),
        )
        assert card["label"] == "Doubao Seed 2.1 Pro"
        assert card["context_size"] == 256_000
        assert card["output_size"] == 32_768
        assert card["status"] == "active"
        assert card["parameter_overrides"]["voice"]["hidden"] is True

    def test_unknown_model_gets_conservative_defaults(self, cards_dir):
        ark._write_chat_cards(["brand-new-model-270101"])
        card = yaml.safe_load(
            (cards_dir / "chat" / "brand-new-model-270101.yaml").read_text(),
        )
        assert card["context_size"] == 131_072
        assert card["output_size"] == 16_384

    def test_rewrite_clears_stale_cards(self, cards_dir):
        ark._write_chat_cards(["model-a", "model-b"])
        assert len(list((cards_dir / "chat").glob("*.yaml"))) == 2
        # 下架 model-a 后同步，旧卡必须被清除
        ark._write_chat_cards(["model-b"])
        names = [p.stem for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["model-b"]

    def test_embedding_cards_written(self, cards_dir):
        ark._write_model_cards()
        emb = list((cards_dir / "embedding").glob("*.yaml"))
        assert len(emb) == len(ark._EMBEDDING_MODELS)
        card = yaml.safe_load((emb[0]).read_text())
        assert card["output_types"] == ["application/x-embedding"]


def _fake_http(monkeypatch, *, status=200, payload=None, exc=None):
    """把 ark 模块里的 httpx.AsyncClient 换成假实现。"""
    payload = payload if payload is not None else {"data": []}

    class FakeResp:
        status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError(
                    "err", request=None, response=None,
                )

        def json(self):
            return payload

    class FakeClient:
        calls = []

        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            type(self).calls.append({"url": url, "headers": headers})
            if exc:
                raise exc
            return FakeResp()

    fake_module = types.SimpleNamespace(AsyncClient=FakeClient)
    monkeypatch.setattr(ark, "httpx", fake_module)
    return FakeClient


class TestSyncArkModels:
    ARK_PAYLOAD = {
        "data": [
            {"id": "doubao-seed-2-1-pro-260628"},
            {"id": "doubao-embedding-large-text-250515"},  # 应被过滤
            {"id": "deepseek-v4-pro-260425"},
            {"id": "totally-unknown-chat-270101"},
        ],
    }

    async def test_sync_success(self, monkeypatch, cards_dir):
        fake = _fake_http(monkeypatch, payload=self.ARK_PAYLOAD)
        n = await ark.sync_ark_models(api_key="test-key")
        # embedding 被过滤 + 未知家族（totally-unknown-*）被家族规则过滤
        assert n == 2
        names = sorted(p.stem for p in (cards_dir / "chat").glob("*.yaml"))
        assert names == [
            "deepseek-v4-pro-260425",
            "doubao-seed-2-1-pro-260628",
        ]
        # 认证头正确
        assert fake.calls[0]["headers"] == {"Authorization": "Bearer test-key"}
        assert fake.calls[0]["url"] == ark.ARK_MODELS_URL

    async def test_sync_without_key_returns_zero(self, monkeypatch, cards_dir):
        monkeypatch.delenv("ARK_API_KEY", raising=False)
        assert await ark.sync_ark_models() == 0

    async def test_sync_failure_keeps_existing_cards(
        self, monkeypatch, cards_dir,
    ):
        ark._write_chat_cards(["existing-model"])
        _fake_http(monkeypatch, status=500)
        n = await ark.sync_ark_models(api_key="k")
        assert n == 0
        names = [p.stem for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["existing-model"]  # 失败不清空

    async def test_sync_network_error_keeps_cards(self, monkeypatch, cards_dir):
        import httpx

        ark._write_chat_cards(["existing-model"])
        _fake_http(monkeypatch, exc=httpx.ConnectError("no network"))
        assert await ark.sync_ark_models(api_key="k") == 0
        assert (cards_dir / "chat" / "existing-model.yaml").exists()

    async def test_uses_env_key_when_not_given(self, monkeypatch, cards_dir):
        fake = _fake_http(monkeypatch, payload={"data": []})
        monkeypatch.setenv("ARK_API_KEY", "env-key")
        await ark.sync_ark_models()
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer env-key"


class TestArkCredentialSchema:
    def test_type_and_defaults(self):
        c = ark.ArkCredential(api_key="k")
        assert c.type == "ark_credential"
        assert c.base_url == "https://ark.cn-beijing.volces.com/api/v3"
        assert c.organization is None

    def test_title_for_ui(self):
        assert ark.ArkCredential.model_config["title"] == "豆包 ARK"

    def test_model_classes_bound(self):
        assert ark.ArkCredential.get_chat_model_class() is ark.ArkChatModel
        assert ark.ArkCredential.get_embedding_model_class() is ark.ArkEmbeddingModel

    def test_no_tts_models(self):
        assert ark.ArkCredential.get_tts_model_classes() == []
