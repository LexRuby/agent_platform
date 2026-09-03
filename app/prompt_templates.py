"""提示词模板：创建 Agent 时可选的起始 system prompt。

- ``GET /prompt-templates``：返回模板列表（name / description / content）
- :class:`PromptTemplateSchemaMiddleware`：把模板注入 ``/agent/schema/v2``
  响应中 ``system_prompt`` 属性的 ``prompt_templates`` 键。官方 Web UI 的
  表单是 schema 驱动的（前端 SchemaForm 识别该键后在文本域上方渲染
  「从模板选择」下拉，选中即填充）。

模板文件放在 ``prompt_templates/*.yaml``（可用环境变量
``AGENTFORGE_PROMPT_TEMPLATES_DIR`` 覆盖），格式::

    name: 团队主理人
    description: 规划任务、组建团队、分派工作的领导者
    content: |
      你是……

收集到新模板后放入该目录即可，无需改代码。
"""

import json
import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter

_logger = logging.getLogger("agentforge.prompt_templates")

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_TEMPLATES_DIR = _BASE_DIR / "prompt_templates"

# 注入目标：/agent/schema/v2 响应中 schema.properties 的哪个字段
SCHEMA_TARGET_FIELD = "system_prompt"


def _templates_dir() -> Path:
    """模板目录：环境变量优先，默认仓库内 prompt_templates/。"""
    return Path(
        os.environ.get(
            "AGENTFORGE_PROMPT_TEMPLATES_DIR", str(_DEFAULT_TEMPLATES_DIR),
        ),
    )


def list_prompt_templates(templates_dir: str | Path | None = None) -> list[dict]:
    """加载全部提示词模板。

    按文件名排序保证列表稳定；单个文件损坏只跳过并告警，不影响整体。

    Returns:
        ``[{"name": ..., "description": ..., "content": ...}, ...]``
    """
    d = Path(templates_dir) if templates_dir else _templates_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            name = str(data.get("name") or p.stem).strip()
            content = str(data.get("content") or "").strip()
            if not name or not content:
                raise ValueError("name / content 不能为空")
            out.append({
                "name": name,
                "description": str(data.get("description") or "").strip(),
                "content": content,
            })
        except Exception as e:  # noqa: BLE001 - 单文件损坏跳过不影响整体
            _logger.warning("提示词模板加载失败（跳过）%s: %s", p.name, e)
    return out


prompt_templates_router = APIRouter(tags=["prompt-templates"])


@prompt_templates_router.get(
    "/prompt-templates",
    summary="提示词模板列表（创建 Agent 时可选的起始 system prompt）",
)
async def get_prompt_templates() -> dict:
    return {"templates": list_prompt_templates()}


class PromptTemplateSchemaMiddleware:
    """把提示词模板注入 ``GET /agent/schema/v2`` 的响应体。

    纯 ASGI 中间件：仅改写该端点 200 JSON 响应，在
    ``schema.properties.system_prompt`` 下追加 ``prompt_templates``；
    其余请求原样透传。模板列表为空时不改写（保持官方原始 schema）。
    """

    _SCHEMA_PATH = "/agent/schema/v2"

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "GET"
            or scope.get("path") != self._SCHEMA_PATH
        ):
            await self.app(scope, receive, send)
            return

        templates = list_prompt_templates()
        if not templates:
            await self.app(scope, receive, send)
            return

        state = {"start": None, "chunks": []}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
                return
            if message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                await self._send_rewritten(state, templates, send)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    async def _send_rewritten(state: dict, templates: list, send) -> None:
        """改写累积完成的响应体并发出（content-length 同步修正）。"""
        start = state["start"] or {
            "type": "http.response.start", "status": 500, "headers": [],
        }
        body = b"".join(state["chunks"])
        status = start.get("status", 500)
        headers = list(start.get("headers", []))
        content_type = next(
            (v.decode() for k, v in headers if k.decode().lower() == "content-type"),
            "json",
        )
        if status == 200 and "json" in content_type:
            body = PromptTemplateSchemaMiddleware._inject(body, templates)
        # content-length 随改写后的 body 修正
        headers = [
            (k, v) for k, v in headers
            if k.decode().lower() != "content-length"
        ]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": body})

    @classmethod
    def _inject(cls, body: bytes, templates: list[dict]) -> bytes:
        """把 templates / agent_type 写进 schema，失败则原样返回。

        注入两处扩展字段（官方 Web UI 按 schema 动态渲染表单）：
        - ``system_prompt.prompt_templates``：提示词模板下拉
        - ``agent_type``：大A / 小A 类型下拉（与 AgentTypeMiddleware 配套）
        """
        try:
            data = json.loads(body)
            props = data["schema"]["properties"]
            props[SCHEMA_TARGET_FIELD]["prompt_templates"] = templates
            from app.agent_type import agent_type_schema_property
            props.setdefault(
                "agent_type", agent_type_schema_property(),
            )
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        except Exception as e:  # noqa: BLE001 - 注入失败保持原始 schema
            _logger.warning("提示词模板注入 schema 失败: %s", e)
            return body
