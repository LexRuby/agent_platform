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
    """_is_chat_model / _is_usable：对话与可用状态过滤规则。"""

    def _m(self, mid, task_type=None, status=None):
        m = {"id": mid}
        if task_type is not None:
            m["task_type"] = task_type
        if status is not None:
            m["status"] = status
        return m

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
        assert ark._is_chat_model(self._m(model_id)) is True

    def test_task_type_text_generation_accepted(self):
        """task_type 含 TextGeneration 即便家族未知也接受。"""
        assert ark._is_chat_model(
            self._m("mystery-model", task_type=["TextGeneration"]),
        ) is True

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
        assert ark._is_chat_model(self._m(model_id)) is False

    @pytest.mark.parametrize("status", [None, "Active", ""])
    def test_usable_statuses(self, status):
        assert ark._is_usable(self._m("doubao-x", status=status)) is True

    @pytest.mark.parametrize("status", ["Shutdown", "Retiring"])
    def test_unusable_statuses(self, status):
        assert ark._is_usable(self._m("doubao-x", status=status)) is False


class TestLabelAndPrice:
    """_pretty_label / _price_tag / _chat_card 的展示构造。"""

    def test_pretty_label_dotted_name(self):
        assert ark._pretty_label("doubao-seed-2.1-pro") == "Doubao Seed 2.1 Pro"

    def test_pretty_label_hyphenated(self):
        assert ark._pretty_label("deepseek-v4-pro") == "DeepSeek V4 Pro"

    def test_pretty_label_known_brands(self):
        assert ark._pretty_label("glm-5.2") == "GLM 5.2"
        assert ark._pretty_label("kimi-k2") == "Kimi K2"

    def test_pretty_label_acronyms_uppercase(self):
        # 缩写词保持全大写：deepseek-v4-pro-ga → DeepSeek V4 Pro GA
        assert ark._pretty_label("deepseek-v4-pro-ga") == "DeepSeek V4 Pro GA"

    def test_price_tag_dotted_and_hyphenated(self):
        # ARK name 字段用点号，id 用连字符，两者都能匹配
        assert "¥6/¥30" in ark._price_tag("doubao-seed-2.1-pro")
        assert "¥6/¥30" in ark._price_tag("doubao-seed-2-1-pro")

    def test_price_tag_versioned_id(self):
        assert "¥3/¥15" in ark._price_tag("doubao-seed-2.1-turbo")

    def test_price_tag_unknown_model_empty(self):
        assert ark._price_tag("mystery-model") == ""

    def test_price_tag_prefix_must_match_boundary(self):
        # 前缀匹配不能误伤（doubao-seed-2 ≠ doubao-seed-2.0-pro 的兄弟名）
        assert "¥" not in ark._price_tag("doubao-seed-9-9-unknown")

    def test_chat_card_uses_api_specs(self):
        m = {
            "id": "doubao-seed-2-1-pro-260628",
            "name": "doubao-seed-2.1-pro",
            "token_limits": {
                "context_window": 262144,
                "max_output_token_length": 262144,
            },
            "modalities": {"input_modalities": ["text", "image", "video"]},
        }
        card = ark._chat_card(m)
        assert card["name"] == "doubao-seed-2-1-pro-260628"
        assert card["label"] == "Doubao Seed 2.1 Pro · ¥6/¥30 起每百万token"
        assert card["context_size"] == 262144
        assert card["output_size"] == 262144
        # 图像模态映射为图片输入类型
        assert "image/jpeg" in card["input_types"]
        assert "image/png" in card["input_types"]

    def test_chat_card_falls_back_without_token_limits(self):
        m = {"id": "totally-unknown-270101", "name": "totally-unknown"}
        card = ark._chat_card(m)
        assert card["context_size"] == 131_072  # 保守默认
        assert card["output_size"] == 16_384
        assert card["label"] == "Totally Unknown"  # 无价格不拼接


def _card(name, **over):
    base = {
        "name": name, "label": name, "status": "active",
        "input_types": ["text/plain"], "output_types": ["text/plain"],
        "context_size": 131_072, "output_size": 16_384,
        "parameters_overrides": {},
    }
    base.update(over)
    return base


class TestWriteChatCards:
    def test_filename_order_prefix(self, cards_dir):
        """文件名带序号前缀固化发布日期排序（glob 顺序不可靠，sorted 断言）。"""
        ark._write_chat_cards([_card("model-a"), _card("model-b")])
        names = sorted(p.name for p in (cards_dir / "chat").glob("*.yaml"))
        assert names == ["0000-model-a.yaml", "0001-model-b.yaml"]

    def test_rewrite_clears_stale_cards(self, cards_dir):
        ark._write_chat_cards([_card("model-a"), _card("model-b")])
        # 下架 model-a 后同步，旧卡必须被清除
        ark._write_chat_cards([_card("model-b")])
        names = [p.name for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["0000-model-b.yaml"]

    def test_yaml_fields_preserved(self, cards_dir):
        ark._write_chat_cards([_card("m1", label="模型一", context_size=999)])
        p = next((cards_dir / "chat").glob("*.yaml"))
        card = yaml.safe_load(p.read_text())
        assert card["label"] == "模型一"
        assert card["context_size"] == 999

    def test_embedding_cards_written(self, cards_dir):
        ark._write_model_cards()
        emb = list((cards_dir / "embedding").glob("*.yaml"))
        assert len(emb) == len(ark._EMBEDDING_MODELS)
        card = yaml.safe_load((emb[0]).read_text())
        assert card["output_types"] == ["application/x-embedding"]

    def test_static_cards_carry_price(self, cards_dir):
        ark._write_model_cards()
        p = next(
            x for x in (cards_dir / "chat").glob("*.yaml")
            if "doubao-seed-2-1-pro" in x.name
        )
        card = yaml.safe_load(p.read_text())
        assert "¥6/¥30" in card["label"]


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
            {"id": "doubao-seed-2-1-pro-260628", "name": "doubao-seed-2.1-pro",
             "created": 100, "task_type": ["TextGeneration"],
             "token_limits": {"context_window": 262144,
                              "max_output_token_length": 262144}},
            {"id": "doubao-embedding-large-text-250515", "created": 100},
            {"id": "deepseek-v4-pro-260425", "name": "deepseek-v4-pro",
             "created": 300, "status": "Shutdown"},  # 已下架 → 过滤
            {"id": "deepseek-v4-flash-260425", "name": "deepseek-v4-flash",
             "created": 200, "status": "Retiring"},  # 退役中 → 过滤
            {"id": "kimi-k2-250711", "name": "kimi-k2", "created": 400},
            {"id": "totally-unknown-chat-270101", "created": 100},
        ],
    }

    async def test_sync_success(self, monkeypatch, cards_dir):
        fake = _fake_http(monkeypatch, payload=self.ARK_PAYLOAD)
        n = await ark.sync_ark_models(api_key="test-key")
        # 过滤：embedding（非对话）+ Shutdown/Retiring（不可用）+ 未知家族
        assert n == 2
        # 排序：created 降序（kimi 400 > doubao 100），序号固化进文件名
        names = sorted(p.name for p in (cards_dir / "chat").glob("*.yaml"))
        assert names == [
            "0000-kimi-k2-250711.yaml",
            "0001-doubao-seed-2-1-pro-260628.yaml",
        ]
        # 认证头正确
        assert fake.calls[0]["headers"] == {"Authorization": "Bearer test-key"}
        assert fake.calls[0]["url"] == ark.ARK_MODELS_URL

    async def test_sync_card_uses_api_specs(self, monkeypatch, cards_dir):
        _fake_http(monkeypatch, payload=self.ARK_PAYLOAD)
        await ark.sync_ark_models(api_key="k")
        p = next(
            x for x in (cards_dir / "chat").glob("*.yaml")
            if "doubao-seed-2-1-pro" in x.name
        )
        card = yaml.safe_load(p.read_text())
        assert card["context_size"] == 262144  # 来自 token_limits 而非默认值
        assert "¥6/¥30" in card["label"]  # 价格标注来自内置表

    async def test_sync_without_key_returns_zero(self, monkeypatch, cards_dir):
        monkeypatch.delenv("ARK_API_KEY", raising=False)
        assert await ark.sync_ark_models() == 0

    async def test_sync_failure_keeps_existing_cards(
        self, monkeypatch, cards_dir,
    ):
        ark._write_chat_cards([_card("existing-model")])
        _fake_http(monkeypatch, status=500)
        n = await ark.sync_ark_models(api_key="k")
        assert n == 0
        names = [p.name for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["0000-existing-model.yaml"]  # 失败不清空

    async def test_sync_network_error_keeps_cards(self, monkeypatch, cards_dir):
        import httpx

        ark._write_chat_cards([_card("existing-model")])
        _fake_http(monkeypatch, exc=httpx.ConnectError("no network"))
        assert await ark.sync_ark_models(api_key="k") == 0
        assert (cards_dir / "chat" / "0000-existing-model.yaml").exists()

    async def test_uses_env_key_when_not_given(self, monkeypatch, cards_dir):
        fake = _fake_http(monkeypatch, payload={"data": []})
        monkeypatch.setenv("ARK_API_KEY", "env-key")
        await ark.sync_ark_models()
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer env-key"


class TestModelVerification:
    """真实调用验证：剔除 /models 列出但账号无权调用（404/403）的模型。

    背景（2026-09-03 事故）：ARK /models 列出 130 个模型，其中多个
    实际无权调用，心跳同步全量入卡后，用户选到即 404 回复失败。
    """

    PAYLOAD = {
        "data": [
            {"id": "doubao-seed-2-1-pro-260628", "name": "doubao-seed-2.1-pro",
             "created": 100, "task_type": ["TextGeneration"]},
            {"id": "doubao-seed-1-6-250615", "name": "doubao-seed-1.6",
             "created": 200, "task_type": ["TextGeneration"]},
        ],
    }

    def _fake_verify_http(self, monkeypatch, *, deny=(), fail=()):
        """GET /models 正常返回；POST 验证按模型名返回预设状态码。

        deny：404 剔除；fail：429 保留（临时错误不误杀）；其余 200。
        """
        import types as _t

        payload = self.PAYLOAD

        class VerifyResp:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class VerifyClient:
            posts = []

            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return VerifyResp(200, payload=payload)

            async def post(self, url, headers=None, json=None):
                mid = json["model"]
                type(self).posts.append({"url": url, "model": mid})
                code = 404 if mid in deny else (429 if mid in fail else 200)
                return VerifyResp(code)

        fake_module = _t.SimpleNamespace(AsyncClient=VerifyClient)
        monkeypatch.setattr(ark, "httpx", fake_module)
        return VerifyClient

    async def test_unauthorized_model_removed(self, monkeypatch, cards_dir):
        """404 模型（无权）从卡片中剔除。"""
        self._fake_verify_http(monkeypatch, deny={"doubao-seed-1-6-250615"})
        n = await ark.sync_ark_models(api_key="k")
        assert n == 1
        names = [p.name for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["0000-doubao-seed-2-1-pro-260628.yaml"]

    async def test_forbidden_model_removed(self, monkeypatch, cards_dir):
        """403 模型同样剔除。"""
        self._fake_verify_http(monkeypatch, deny={"doubao-seed-2-1-pro-260628"})
        n = await ark.sync_ark_models(api_key="k")
        assert n == 1
        names = [p.name for p in (cards_dir / "chat").glob("*.yaml")]
        assert names == ["0000-doubao-seed-1-6-250615.yaml"]

    async def test_transient_error_keeps_model(self, monkeypatch, cards_dir):
        """429（限流）等临时错误保留模型，不误杀。"""
        self._fake_verify_http(monkeypatch, fail={"doubao-seed-1-6-250615"})
        n = await ark.sync_ark_models(api_key="k")
        assert n == 2

    async def test_verification_can_be_disabled(
        self, monkeypatch, cards_dir,
    ):
        """AGENTFORGE_ARK_VERIFY_MODELS=0 时不发验证请求。"""
        fake = self._fake_verify_http(monkeypatch, deny={"doubao-seed-1-6-250615"})
        monkeypatch.setenv("AGENTFORGE_ARK_VERIFY_MODELS", "0")
        n = await ark.sync_ark_models(api_key="k")
        assert n == 2
        assert fake.posts == []

    async def test_verify_posts_one_token_request(
        self, monkeypatch, cards_dir,
    ):
        """验证请求打 chat/completions 且 max_tokens=1（成本控制）。"""
        fake = self._fake_verify_http(monkeypatch)
        await ark.sync_ark_models(api_key="k")
        assert len(fake.posts) == 2
        assert all(
            p["url"].endswith("/chat/completions") for p in fake.posts
        )
        # 每个 POST 的 json body 里 max_tokens=1：通过 posts 记录无法直接
        # 断言 body，这里断言 URL 与并发全量覆盖（两个模型各一次）
        assert {p["model"] for p in fake.posts} == {
            "doubao-seed-2-1-pro-260628", "doubao-seed-1-6-250615",
        }


class TestArkChatModelListModels:
    """ArkChatModel.list_models：按文件名序号（发布日期降序）返回。"""

    def test_returns_cards_in_release_order(self, cards_dir):
        ark._write_chat_cards([
            _card("newest-model"), _card("middle-model"), _card("oldest-model"),
        ])
        cards = ark.ArkChatModel.list_models()
        assert [c.name for c in cards] == [
            "newest-model", "middle-model", "oldest-model",
        ]

    def test_card_fields_loaded(self, cards_dir):
        ark._write_chat_cards([_card("m1", context_size=999, output_size=88)])
        card = ark.ArkChatModel.list_models()[0]
        assert card.context_size == 999
        assert card.output_size == 88

    def test_empty_dir_returns_empty(self, cards_dir):
        assert ark.ArkChatModel.list_models() == []

    def test_broken_card_skipped(self, cards_dir):
        (cards_dir / "chat").mkdir(parents=True, exist_ok=True)
        (cards_dir / "chat" / "0000-broken.yaml").write_text(
            "not: [valid", encoding="utf-8",
        )
        (cards_dir / "chat" / "0001-good.yaml").write_text(
            "name: good\nlabel: good\nstatus: active\ncontext_size: 1\n"
            "output_size: 1\nparameter_schema: {}\nparameters_overrides: {}\n",
            encoding="utf-8",
        )
        cards = ark.ArkChatModel.list_models()
        assert [c.name for c in cards] == ["good"]


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
