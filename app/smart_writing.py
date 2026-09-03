"""智能写作能力 MCP Server（无状态 streamable HTTP，JSON-RPC 2.0）。

注册源：``mcp_registry/smart_writing.json``（用户整理的机读注册包，
原稿见 AgentScope/mcp_data/2026-09-03_MCP注册规格-智能写作.json）。

- 9 个工具的 name / description / inputSchema **原样透传**给调用方
  （文档注意事项 1：description 里的"何时用/何时不用"负例直接决定
  Agent 选工具的准确率，禁止压缩改写）
- 调用时按 ``x_backend[].when`` 路由（mode=natural/boolean、
  action=filter/cluster/save/list/fetch、always），只把 ``query_fields``
  列出的入参组装进 ``utterance.query``，POST 统一信封::

      {"utterance": {"query": {...}}, "method": "<后端方法名>", "source": "vr"}

- 响应取 ``response.data``（搜索类附带 ``response.cnt_dict``）；
  ``error_msg`` 存在时原文透传给 Agent，便于其自行修正参数重试
  （注意事项 3）
- 条件必填校验（注意事项 2，JSON Schema 表达不了的运行时校验）：
  filter_solutions 在 action=filter 时 query 必填；user_results 按
  action 要求 name/time/info
- 超时（注意事项 4）：extract_solutions / filter_solutions 是批量
  LLM 并发任务 ≥300s，其余 60s

无状态 MCP：不签发 Mcp-Session-Id，每次请求独立处理——标准 MCP
客户端（含 agentscope 内置 HttpMCPConfig）兼容此模式。

启动：uvicorn app.smart_writing:mcp_app --port 30110
"""

import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_logger = logging.getLogger("agentforge.smart_writing")

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_REGISTRY = _BASE_DIR / "mcp_registry" / "smart_writing.json"

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "smart-writing", "version": "1.0.0"}

# 批量 LLM 并发任务（注意事项 4：耗时随批量增长，超时 ≥ 300s）
_SLOW_TOOLS = {"extract_solutions", "filter_solutions"}
_SLOW_TIMEOUT = 320.0
_DEFAULT_TIMEOUT = 60.0

# 条件必填（注意事项 2）：工具名 → (参数名, 触发校验的入参条件, 报错文案)
_CONDITIONAL_REQUIRED = {
    "filter_solutions": [
        (
            "query",
            lambda a: a.get("action") == "filter",
            "action=filter 时必须提供 query（待解决技术问题）",
        ),
    ],
    "user_results": [
        (
            "name",
            lambda a: a.get("action") in ("save", "fetch"),
            "action={action} 时必须提供 name（存档名）",
        ),
        (
            "time",
            lambda a: a.get("action") == "save",
            "action=save 时必须提供 time（格式 YYYY-MM-DD HH:MM:SS）",
        ),
        (
            "info",
            lambda a: a.get("action") == "save",
            "action=save 时必须提供 info（存档内容字符串）",
        ),
    ],
}


def _registry_path() -> Path:
    import os

    return Path(
        os.environ.get(
            "AGENTFORGE_SMART_WRITING_REGISTRY", str(_DEFAULT_REGISTRY),
        ),
    )


def load_tools(path: Path | None = None) -> list[dict]:
    """读注册包 tools；结构非法直接抛错（启动即失败，不带病上线）。"""
    p = path or _registry_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    tools = data.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError(f"注册包无有效 tools: {p}")
    for t in tools:
        for field in ("name", "description", "inputSchema"):
            if field not in t:
                raise ValueError(f"注册包工具缺字段 {field}: {t.get('name')}")
        if not t.get("x_backend"):
            raise ValueError(f"注册包工具缺 x_backend 路由: {t['name']}")
    return tools


def route_backend(tool: dict, arguments: dict) -> dict:
    """按 x_backend[].when 匹配路由；无命中抛 RuntimeError。

    when 形如 ``mode=natural`` / ``action=save`` / ``always``，
    等号左侧是入参名，右侧是期望值。
    """
    for routing in tool["x_backend"]:
        cond = routing.get("when", "always")
        if cond == "always":
            return routing
        key, _, expected = cond.partition("=")
        if str(arguments.get(key, "")) == expected:
            return routing
    raise RuntimeError(
        f"工具 {tool['name']} 无匹配路由（检查 mode/action 入参取值）",
    )


def build_envelope(routing: dict, arguments: dict) -> dict:
    """query_fields 过滤 + 统一信封。未列入 query_fields 的入参被丢弃。"""
    query = {
        f: arguments[f]
        for f in routing.get("query_fields", [])
        if f in arguments
    }
    return {
        "utterance": {"query": query},
        "method": routing["method"],
        "source": "vr",
    }


def validate_conditional_required(tool_name: str, arguments: dict) -> str | None:
    """返回中文错误文案；None 表示通过。"""
    for param, trigger, msg in _CONDITIONAL_REQUIRED.get(tool_name, []):
        if trigger(arguments) and not arguments.get(param):
            return msg.format(action=arguments.get("action", ""))
    return None


def parse_response(tool_name: str, body: dict) -> dict:
    """后端响应 → 业务载荷。error_msg 原文透传（注意事项 3）。

    - patent_search 另带 cnt_dict 字段统计（信封文档）；
    - 后端返回 error_msg（如 "no skill returned any result"）时不静默
      吞掉，抛给 Agent 让其自行修正参数重试。
    """
    if not isinstance(body, dict):
        return {"raw": body}
    response = body.get("response")
    if not isinstance(response, dict):
        err = body.get("error_msg") or json.dumps(body, ensure_ascii=False)[:500]
        raise RuntimeError(f"后端返回错误: {err}")
    if body.get("error_msg"):
        raise RuntimeError(f"后端返回错误: {body['error_msg']}")
    payload = {"data": response.get("data")}
    if isinstance(response.get("cnt_dict"), dict):
        payload["cnt_dict"] = response["cnt_dict"]
    return payload


async def call_backend(
    tool: dict, arguments: dict, *, client: httpx.AsyncClient | None = None,
) -> dict:
    """完整调用链：条件必填 → 路由 → 信封 → POST → 解析。

    ``client`` 可注入（测试用 MockTransport）；默认按工具超时配置
    新建一次性连接。
    """
    err = validate_conditional_required(tool["name"], arguments)
    if err:
        raise RuntimeError(err)
    routing = route_backend(tool, arguments)
    envelope = build_envelope(routing, arguments)
    if client is not None:
        resp = await client.post(routing["endpoint"], json=envelope)
    else:
        timeout = _SLOW_TIMEOUT if tool["name"] in _SLOW_TOOLS else _DEFAULT_TIMEOUT
        async with httpx.AsyncClient(timeout=timeout) as one_shot:
            resp = await one_shot.post(routing["endpoint"], json=envelope)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"后端 HTTP {resp.status_code}: {resp.text[:300]}",
        )
    return parse_response(tool["name"], resp.json())


# ── 无状态 MCP 端点 ────────────────────────────────────────────────────

mcp_app = FastAPI(title="smart-writing-mcp", docs_url=None, redoc_url=None)


def _result(request_id, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


@mcp_app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    body = await request.json()
    method = body.get("method", "")
    request_id = body.get("id")

    # 通知（notifications/initialized 等）：202 空响应即可
    if request_id is None:
        return Response(status_code=202)

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method == "tools/list":
        tools = load_tools()
        return _result(request_id, {
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                }
                for t in tools
            ],
        })

    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        tools = {t["name"]: t for t in load_tools()}
        tool = tools.get(name)
        if tool is None:
            return _error(request_id, -32602, f"未知工具: {name}")
        try:
            payload = await call_backend(tool, arguments)
        except RuntimeError as e:
            # 工具执行错误：MCP isError 语义，文本透传（注意事项 3）
            return _result(request_id, {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            })
        except Exception as e:  # noqa: BLE001 - 网络/解析异常也透传给 Agent
            _logger.exception("smart_writing 调用异常: %s", name)
            return _result(request_id, {
                "content": [{"type": "text", "text": f"调用失败: {e}"}],
                "isError": True,
            })
        return _result(request_id, {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
            ],
        })

    return _error(request_id, -32601, f"未知方法: {method}")


@mcp_app.get("/mcp")
async def mcp_get() -> Response:
    """无状态 server 不提供 SSE 通道；标准客户端收到 405 后自动走 POST。"""
    return Response(status_code=405)


@mcp_app.get("/health")
async def health() -> dict:
    """部署健康检查 + 注册包自检。"""
    tools = load_tools()
    return {"ok": True, "tools": [t["name"] for t in tools]}


def main() -> None:
    """独立进程入口：uvicorn :30110。"""
    import os

    import uvicorn

    port = int(os.environ.get("AGENTFORGE_SMART_WRITING_PORT", "30110"))
    uvicorn.run(mcp_app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
