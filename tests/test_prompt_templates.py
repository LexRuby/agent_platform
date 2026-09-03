"""prompt_templates 模块测试：模板加载、/prompt-templates API、schema 注入中间件。

测试隔离原则：
- 模板目录 → tmp_path（不读仓库真实 prompt_templates/，除种子结构测试外）
- HTTP → starlette TestClient（ASGI 进程内调用，不占端口、不依赖 Redis）
"""

import json

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.prompt_templates import (
    SCHEMA_TARGET_FIELD,
    PromptTemplateSchemaMiddleware,
    list_prompt_templates,
    prompt_templates_router,
)

SCHEMA_PATH = "/agent/schema/v2"


def _write_template(d, filename, name=None, description=None, content=None):
    """写一个模板 YAML；字段缺省时给合法默认值。"""
    data = {
        "name": name,
        "description": description,
        "content": content,
    }
    (d / filename).write_text(
        yaml.safe_dump({k: v for k, v in data.items() if v is not None},
                       allow_unicode=True),
        encoding="utf-8",
    )


@pytest.fixture
def tpl_dir(tmp_path):
    d = tmp_path / "prompt_templates"
    d.mkdir()
    return d


@pytest.fixture
def env_tpl_dir(tpl_dir, monkeypatch):
    """把模块的模板目录环境变量指到 tmp_path。"""
    monkeypatch.setenv("AGENTFORGE_PROMPT_TEMPLATES_DIR", str(tpl_dir))
    return tpl_dir


class TestListPromptTemplates:
    """模板加载器。"""

    def test_load_sorted_by_filename(self, tpl_dir):
        _write_template(tpl_dir, "b.yaml", name="乙", content="内容乙")
        _write_template(tpl_dir, "a.yaml", name="甲", content="内容甲")
        out = list_prompt_templates(tpl_dir)
        assert [t["name"] for t in out] == ["甲", "乙"]

    def test_fields_complete(self, tpl_dir):
        _write_template(
            tpl_dir, "x.yaml",
            name="专家", description="某领域专家", content="你是专家",
        )
        out = list_prompt_templates(tpl_dir)
        assert out == [{
            "name": "专家", "description": "某领域专家", "content": "你是专家",
        }]

    def test_description_defaults_empty(self, tpl_dir):
        _write_template(tpl_dir, "x.yaml", name="无描述", content="内容")
        out = list_prompt_templates(tpl_dir)
        assert out[0]["description"] == ""

    def test_name_falls_back_to_stem(self, tpl_dir):
        # 缺 name 时用文件名（去扩展名）作为模板名
        _write_template(tpl_dir, "fallback.yaml", content="内容")
        out = list_prompt_templates(tpl_dir)
        assert out[0]["name"] == "fallback"

    def test_skip_invalid_files(self, tpl_dir):
        # 语法错误 / 缺 content —— 只跳过，不影响其余模板
        # （缺 name 会回退到文件名，属合法模板，见 test_name_falls_back_to_stem）
        (tpl_dir / "bad-syntax.yaml").write_text(
            "name: [unclosed", encoding="utf-8",
        )
        _write_template(tpl_dir, "no-content.yaml", name="没有内容")
        _write_template(tpl_dir, "good.yaml", name="正常", content="内容")
        out = list_prompt_templates(tpl_dir)
        assert [t["name"] for t in out] == ["正常"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert list_prompt_templates(tmp_path / "nope") == []

    def test_env_var_overrides_dir(self, tpl_dir, monkeypatch):
        monkeypatch.setenv("AGENTFORGE_PROMPT_TEMPLATES_DIR", str(tpl_dir))
        _write_template(tpl_dir, "x.yaml", name="环境变量", content="内容")
        assert [t["name"] for t in list_prompt_templates()] == ["环境变量"]

    def test_seed_templates_structure(self):
        """仓库自带种子模板：结构合法、名称唯一（内容允许随后续收集变动）。"""
        out = list_prompt_templates()
        assert len(out) >= 1
        names = [t["name"] for t in out]
        assert len(names) == len(set(names)), "模板名称必须唯一"
        for t in out:
            assert t["name"] and t["content"]
            assert isinstance(t["description"], str)


class TestPromptTemplatesApi:
    """GET /prompt-templates 端点。"""

    @pytest.fixture
    def client(self, env_tpl_dir):
        app = FastAPI()
        app.include_router(prompt_templates_router)
        return TestClient(app)

    def test_returns_templates(self, client, tpl_dir):
        _write_template(tpl_dir, "a.yaml", name="甲", content="内容甲")
        r = client.get("/prompt-templates")
        assert r.status_code == 200
        assert r.json() == {"templates": [{
            "name": "甲", "description": "", "content": "内容甲",
        }]}

    def test_empty_dir_returns_empty_list(self, client):
        r = client.get("/prompt-templates")
        assert r.status_code == 200
        assert r.json() == {"templates": []}


def _make_inner_app(status=200, body=b"", chunked=False,
                    content_type=b"application/json"):
    """构造最小 ASGI 应用：按给定状态/类型/分片策略回一个响应。"""
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type)],
        })
        if chunked and body:
            half = len(body) // 2
            await send({
                "type": "http.response.body",
                "body": body[:half], "more_body": True,
            })
            await send({"type": "http.response.body", "body": body[half:]})
        else:
            await send({"type": "http.response.body", "body": body})

    return app


def _schema_body() -> bytes:
    """模拟 /agent/schema/v2 的响应结构（只保留关心的部分）。"""
    return json.dumps({
        "schema": {
            "properties": {
                "name": {"type": "string", "title": "Name"},
                SCHEMA_TARGET_FIELD: {
                    "type": "string", "format": "textarea", "title": "System Prompt",
                },
            },
        },
    }).encode("utf-8")


@pytest.fixture
def two_templates(env_tpl_dir, tpl_dir):
    _write_template(
        tpl_dir, "a.yaml", name="甲", description="描述甲", content="内容甲",
    )
    _write_template(tpl_dir, "b.yaml", name="乙", content="内容乙")


class TestSchemaMiddleware:
    """PromptTemplateSchemaMiddleware：仅改写 /agent/schema/v2 的 200 JSON 响应。"""

    def test_injects_into_system_prompt(self, two_templates):
        app = PromptTemplateSchemaMiddleware(_make_inner_app(body=_schema_body()))
        r = TestClient(app).get(SCHEMA_PATH)
        assert r.status_code == 200
        prop = r.json()["schema"]["properties"][SCHEMA_TARGET_FIELD]
        assert prop["format"] == "textarea"  # 原有属性保留
        assert prop["prompt_templates"] == [
            {"name": "甲", "description": "描述甲", "content": "内容甲"},
            {"name": "乙", "description": "", "content": "内容乙"},
        ]

    def test_content_length_matches_body(self, two_templates):
        app = PromptTemplateSchemaMiddleware(_make_inner_app(body=_schema_body()))
        r = TestClient(app).get(SCHEMA_PATH)
        assert int(r.headers["content-length"]) == len(r.content)

    def test_chunked_body_is_reassembled(self, two_templates):
        app = PromptTemplateSchemaMiddleware(
            _make_inner_app(body=_schema_body(), chunked=True),
        )
        r = TestClient(app).get(SCHEMA_PATH)
        prop = r.json()["schema"]["properties"][SCHEMA_TARGET_FIELD]
        assert len(prop["prompt_templates"]) == 2

    def test_passthrough_other_path(self, two_templates):
        app = PromptTemplateSchemaMiddleware(
            _make_inner_app(body=b'{"ok": true}'),
        )
        r = TestClient(app).get("/other")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_passthrough_post_method(self, two_templates):
        # 非 GET 方法透传：内层应用原样响应，不做模板注入
        app = PromptTemplateSchemaMiddleware(_make_inner_app(body=_schema_body()))
        r = TestClient(app).post(SCHEMA_PATH)
        assert r.status_code == 200
        prop = r.json()["schema"]["properties"][SCHEMA_TARGET_FIELD]
        assert "prompt_templates" not in prop

    def test_passthrough_when_no_templates(self, env_tpl_dir):
        # 模板目录为空 → 保持官方原始 schema（无 prompt_templates 键）
        app = PromptTemplateSchemaMiddleware(_make_inner_app(body=_schema_body()))
        r = TestClient(app).get(SCHEMA_PATH)
        prop = r.json()["schema"]["properties"][SCHEMA_TARGET_FIELD]
        assert "prompt_templates" not in prop

    def test_passthrough_non_200(self, two_templates):
        app = PromptTemplateSchemaMiddleware(
            _make_inner_app(status=404, body=_schema_body()),
        )
        r = TestClient(app).get(SCHEMA_PATH)
        assert r.status_code == 404
        assert "prompt_templates" not in r.json()[
            "schema"]["properties"][SCHEMA_TARGET_FIELD]

    def test_passthrough_non_json(self, two_templates):
        app = PromptTemplateSchemaMiddleware(
            _make_inner_app(body=b"plain text", content_type=b"text/plain"),
        )
        r = TestClient(app).get(SCHEMA_PATH)
        assert r.text == "plain text"

    def test_malformed_json_body_unchanged(self, two_templates):
        # 响应体不是合法 JSON（或结构不符合预期）→ 原样返回，不抛异常
        app = PromptTemplateSchemaMiddleware(
            _make_inner_app(body=b"{not-json"),
        )
        r = TestClient(app).get(SCHEMA_PATH)
        assert r.content == b"{not-json"

    def test_body_without_target_field_unchanged(self, two_templates):
        body = json.dumps({"schema": {"properties": {}}}).encode()
        app = PromptTemplateSchemaMiddleware(_make_inner_app(body=body))
        r = TestClient(app).get(SCHEMA_PATH)
        assert r.json() == {"schema": {"properties": {}}}

    async def test_non_http_scope_passthrough(self, two_templates):
        """lifespan 等非 http scope 原样透传给内层应用。"""
        called = []

        async def inner(scope, receive, send):
            called.append(scope["type"])

        app = PromptTemplateSchemaMiddleware(inner)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        await app({"type": "lifespan"}, receive, send)
        assert called == ["lifespan"]
