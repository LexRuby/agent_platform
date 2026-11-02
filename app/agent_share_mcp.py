"""Agent Forge 发布物的通用无状态 MCP Streamable HTTP 端点。"""

import html
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .agent_share import _load_share, _PUBMETA_KEY
from .auth import create_scoped_mcp_token, require_cookie_user

PROTOCOL_VERSION = "2025-03-26"
_RUN_KEY = "agentforge:share:mcp-run:{user}:{published_id}:{run_id}"
_SENSITIVE_KEY = re.compile(
    r"token|password|secret|key|credential|auth|cookie", re.IGNORECASE,
)
_ALLOWED_BLOCKS = {"text", "tool_call", "tool_result", "hint"}
_MAX_VALUE = 8000
_MAX_MESSAGES = 5000
_STATUS_PRIORITY = {
    "unknown": 0,
    "idle": 1,
    "awaiting_external_result": 2,
    "awaiting_permission": 3,
    "running": 4,
}

agent_share_mcp_router = APIRouter(tags=["agent-share-mcp"])

_TOOLS = [
    {
        "name": "run_task",
        "description": "异步启动该 Agent Forge 发布物执行任务，立即返回运行标识。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "任务内容"},
                "session_id": {
                    "type": "string",
                    "description": "可选；仅可复用当前账号对此发布物的会话",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_run",
        "description": "查询运行状态、公开可验证行动轨迹与最终答案。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
]


def _result(request_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": code, "message": message},
    })


def _tool_result(payload: dict | str, *, error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    out = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


def _caller(request: Request) -> str:
    return request.headers.get("x-user-id", "")


async def _publication(storage: Any, caller: str, published_id: str) -> tuple[dict, dict] | None:
    client = storage._client  # noqa: SLF001
    raw = await client.get(_PUBMETA_KEY.format(published_id=published_id))
    share = await _load_share(client, published_id)
    if not raw or not share:
        return None
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return None
    owner = share.get("owner") or ""
    allowed = caller == owner or share.get("mode") == "public" or (
        share.get("mode") == "users" and caller in (share.get("users") or [])
    )
    if not allowed:
        return None
    if await storage.get_agent(owner, published_id) is None:
        return None
    return meta, share


async def _official(method: str, path: str, user: str, **kwargs) -> httpx.Response:
    from agent_service_app import _official_app  # noqa: PLC0415

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_official_app),
        base_url="http://internal", timeout=30.0,
    ) as client:
        return await client.request(
            method, path, headers={"X-User-ID": user}, **kwargs,
        )


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _redact(value: Any, key: str = "") -> Any:
    value = _dump(value)
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:200]]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, (dict, list)):
            return json.dumps(_redact(decoded), ensure_ascii=False)
        value = re.sub(r"(?<!\w)/(?:[^\s/]+/)+[^\s]+", "[INTERNAL_PATH]", value)
        if len(value) > _MAX_VALUE:
            return value[:_MAX_VALUE] + "...[TRUNCATED]"
    return value


def _message_window(
    messages: list[Any], start_message_id: str, created_at: str,
    end_message_id: str = "", end_created_at: str = "",
) -> list[Any]:
    dumped = [_dump(message) for message in messages]
    start = None
    end = len(messages)
    for index, message in enumerate(dumped):
        if not isinstance(message, dict):
            continue
        if message.get("id") == start_message_id:
            start = index
        if end_message_id and message.get("id") == end_message_id:
            end = index
            break
    if start is not None:
        return messages[start:end]
    if created_at:
        return [
            raw for raw, message in zip(messages, dumped)
            if isinstance(message, dict)
            and (message.get("created_at") or "") >= created_at
            and (
                not end_created_at
                or (message.get("created_at") or "") < end_created_at
            )
        ]
    return []


def _responsibility(data: Any) -> str:
    cfg = _dump(data) or {}
    if not isinstance(cfg, dict):
        cfg = {
            "invite_config": getattr(data, "invite_config", None),
            "system_prompt": getattr(data, "system_prompt", ""),
        }
    invite = _dump(cfg.get("invite_config")) or {}
    description = (
        invite.get("invite_description", "") if isinstance(invite, dict)
        else getattr(invite, "invite_description", "")
    )
    if description:
        return description
    match = re.search(
        r"Your role:\s*(.*?)(?=\n\nYou communicate|\Z)",
        cfg.get("system_prompt") or "", re.S,
    )
    return match.group(1).strip() if match else ""


def _event_time(event: dict) -> float:
    value = event.get("created_at") or ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return float("inf")


def _public_messages(messages: list[Any], session_id: str) -> tuple[list[dict], str]:
    events: list[dict] = []
    final = ""
    for raw in messages:
        msg = _dump(raw)
        if not isinstance(msg, dict):
            continue
        blocks = []
        for block in msg.get("content") or []:
            block = _dump(block)
            if not isinstance(block, dict) or block.get("type") not in _ALLOWED_BLOCKS:
                continue
            blocks.append(_redact(block))
        if not blocks:
            continue
        event = {
            "evidence_id": msg.get("id") or f"{session_id}:{len(events)}",
            "session_id": session_id,
            "role": msg.get("role"),
            "name": msg.get("name"),
            "content": blocks,
            "created_at": msg.get("created_at"),
            "source": "observed",
        }
        events.append(event)
        if msg.get("role") == "assistant" and msg.get("finished_at") is not None:
            reason = msg.get("finished_reason")
            if reason is None or str(reason).lower() in {"completed", "stop"}:
                texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                if texts:
                    final = "\n".join(texts)
    return events, final


async def _recent_model(storage: Any, user: str) -> dict | None:
    client = storage._client  # noqa: SLF001
    pattern = f"agentscope:user:{user}:session:*"
    records = []
    async for key in client.scan_iter(match=pattern):
        if key.endswith(":messages"):
            continue
        raw = await client.get(key)
        try:
            rec = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        config = rec.get("config") or {}
        if config.get("chat_model_config"):
            records.append(rec)
    records.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    return (records[0].get("config") or {}).get("chat_model_config") if records else None


async def _run_task(storage: Any, caller: str, published_id: str, args: dict) -> dict:
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 必须是非空字符串")
    session_id = args.get("session_id")
    reused_session = bool(session_id)
    if session_id:
        session = await storage.get_session(caller, published_id, session_id)
        if session is None:
            raise ValueError("session_id 不属于当前账号与发布物")
        if session.config.chat_model_config is None:
            raise ValueError("该会话未配置模型")
    else:
        model = await _recent_model(storage, caller)
        if model is None:
            raise ValueError("当前账号没有可复用的模型配置，请先在 Agent Forge 创建并配置一个会话")
        response = await _official(
            "POST", "/sessions/", caller,
            json={"agent_id": published_id, "chat_model_config": model},
        )
        if response.status_code not in (200, 201):
            raise ValueError(f"创建会话失败: HTTP {response.status_code}")
        session_id = response.json()["session_id"]

    run_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    msg = {
        "id": uuid.uuid4().hex, "role": "user", "name": "user",
        "content": [{"type": "text", "text": task.strip()}],
    }
    run = {
        "run_id": run_id, "session_id": session_id, "status": "started",
        "task": task.strip(), "published_id": published_id,
        "start_message_id": msg["id"], "created_at": created_at,
        "reused_session": reused_session,
    }
    await storage._client.set(  # noqa: SLF001
        _RUN_KEY.format(user=caller, published_id=published_id, run_id=run_id),
        json.dumps(run, ensure_ascii=False),
    )
    response = await _official(
        "POST", "/chat/", caller,
        json={"agent_id": published_id, "session_id": session_id, "input": msg},
    )
    if response.status_code != 200:
        await storage._client.delete(  # noqa: SLF001
            _RUN_KEY.format(user=caller, published_id=published_id, run_id=run_id),
        )
        raise ValueError(f"启动任务失败: HTTP {response.status_code}")
    return run


async def _messages(
    storage: Any, user: str, session_id: str,
) -> tuple[list[Any], bool]:
    pages: list[list[Any]] = []
    before: str | None = None
    truncated = False
    total = 0
    while total < _MAX_MESSAGES:
        page, has_more = await storage.list_messages(
            user, session_id, limit=min(200, _MAX_MESSAGES - total), before=before,
        )
        if not page:
            truncated = bool(has_more)
            break
        pages.append(page)
        total += len(page)
        if not has_more:
            break
        oldest = _dump(page[0])
        before = oldest.get("id") if isinstance(oldest, dict) else None
        if not before:
            truncated = True
            break
    else:
        truncated = True
    return [message for page in reversed(pages) for message in page], truncated


async def _session_status(
    caller: str, agent_id: str, session_id: str,
) -> str:
    try:
        response = await _official(
            "GET", f"/sessions/{session_id}/status", caller,
            params={"agent_id": agent_id},
        )
        if response.status_code != 200:
            return "unknown"
        status = response.json().get("status", "unknown")
        return status if status in _STATUS_PRIORITY else "unknown"
    except Exception:  # noqa: BLE001 — 状态异常不能阻断轨迹查询
        return "unknown"


async def _next_run(storage: Any, caller: str, published_id: str,
                    run: dict) -> dict | None:
    client = storage._client  # noqa: SLF001
    pattern = _RUN_KEY.format(
        user=caller, published_id=published_id, run_id="*",
    )
    candidates = []
    async for key in client.scan_iter(match=pattern):
        raw = await client.get(key)
        try:
            other = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if (
            other.get("session_id") == run.get("session_id")
            and (other.get("created_at") or "") > (run.get("created_at") or "")
        ):
            candidates.append(other)
    return min(candidates, key=lambda item: item["created_at"]) if candidates else None


async def _get_run(storage: Any, caller: str, published_id: str, args: dict,
                   meta: dict) -> dict:
    run_id = args.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id 必须是非空字符串")
    raw = await storage._client.get(  # noqa: SLF001
        _RUN_KEY.format(user=caller, published_id=published_id, run_id=run_id),
    )
    if not raw:
        raise ValueError("运行不存在或不属于当前账号与发布物")
    run = json.loads(raw)
    session = await storage.get_session(caller, published_id, run["session_id"])
    if session is None:
        raise ValueError("运行会话不存在")
    leader_status = await _session_status(caller, published_id, session.id)
    next_run = await _next_run(storage, caller, published_id, run)
    all_leader_messages, trajectory_truncated = await _messages(
        storage, caller, session.id,
    )
    leader_messages = _message_window(
        all_leader_messages,
        run.get("start_message_id", ""), run.get("created_at", ""),
        (next_run or {}).get("start_message_id", ""),
        (next_run or {}).get("created_at", ""),
    )
    trajectory, final = _public_messages(leader_messages, session.id)
    members = []
    statuses = [leader_status]
    for team in await storage.list_teams(caller):
        if team.session_id != session.id:
            continue
        for member in team.data.members:
            agent = await storage.get_agent(member.owner_id, member.agent_id)
            data = (
                getattr(agent, "config", None)
                or getattr(agent, "data", None)
                or {}
            )
            member_status = await _session_status(
                caller, member.agent_id, member.session_id,
            )
            statuses.append(member_status)
            data_dict = _dump(data) or {}
            name = (
                data_dict.get("name", "") if isinstance(data_dict, dict)
                else getattr(data, "name", "")
            )
            members.append({
                "agent_id": member.agent_id,
                "session_id": member.session_id,
                "name": name or member.agent_id[:8],
                "role": member.role,
                "responsibility": _responsibility(data),
                "status": member_status,
            })
            member_messages, member_truncated = await _messages(
                storage, caller, member.session_id,
            )
            trajectory_truncated = trajectory_truncated or member_truncated
            member_messages = [
                raw for raw in member_messages
                if (_dump(raw).get("created_at") or "") >= run.get("created_at", "")
                and (
                    next_run is None
                    or (_dump(raw).get("created_at") or "") < next_run["created_at"]
                )
            ]
            events, _ = _public_messages(member_messages, member.session_id)
            trajectory.extend(events)
    trajectory.sort(key=_event_time)
    status_value = max(statuses, key=_STATUS_PRIORITY.get)
    completed = bool(final) and leader_status == "idle" and all(
        status == "idle" for status in statuses
    )
    return {
        "run_id": run_id, "session_id": session.id, "status": status_value,
        "completed": completed,
        "task": run.get("task", ""),
        "publication": {
            "published_id": published_id,
            "source_agent_id": meta.get("source_agent_id", ""),
            "source_version": meta.get("source_version", 0),
            "team_mode": meta.get("team_mode", "blueprint"),
        },
        "team": {
            "leader": {
                "agent_id": published_id, "session_id": session.id,
                "status": leader_status,
            },
            "members": members,
        },
        "trajectory": trajectory,
        "trajectory_truncated": trajectory_truncated,
        "final_answer": final,
    }


def _config_name(value: str, published_id: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE)
    return cleaned.strip("-_")[:64] or f"agent-forge-{published_id[:8]}"


@agent_share_mcp_router.post("/agent-share/mcp/{published_id}/config")
async def create_published_agent_mcp_config(
    published_id: str, request: Request,
) -> dict:
    """Cookie 登录用户为有权访问的发布物签发独立 scoped token。"""
    caller = await require_cookie_user(request)
    publication = await _publication(
        request.app.state.storage, caller, published_id,
    )
    if publication is None:
        return JSONResponse(
            {"detail": "发布物不存在或无权访问"}, status_code=404,
        )
    meta, _share = publication
    endpoint = str(request.base_url).rstrip(
        "/",
    ) + f"/agent-share/mcp/{published_id}"
    token = await create_scoped_mcp_token(caller, published_id)
    name = _config_name(meta.get("display_name") or "", published_id)
    config = {
        "mcpServers": {
            name: {
                "url": endpoint,
                "headers": {"Authorization": f"Bearer {token}"},
            },
        },
    }
    return {"endpoint": endpoint, "config": config}


@agent_share_mcp_router.get(
    "/agent-share/mcp/{published_id}",
    include_in_schema=False,
)
async def published_agent_mcp_guide(
    published_id: str,
    request: Request,
) -> Response:
    """浏览器接入说明页；真正的 MCP 协议请求使用同 URL 的 POST。"""
    caller = _caller(request)
    publication = await _publication(
        request.app.state.storage, caller, published_id,
    )
    if publication is None:
        return JSONResponse(
            {"detail": "发布物不存在或无权访问"}, status_code=404,
        )
    meta, share = publication
    endpoint = str(request.url).split("?", 1)[0]
    title = html.escape(meta.get("display_name") or published_id)
    url = html.escape(endpoint)
    mode = html.escape(share.get("mode") or "users")
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · Agent Forge MCP</title>
<style>
body{{font:15px/1.65 system-ui,sans-serif;max-width:820px;margin:48px auto;padding:0 20px;color:#172033}}
code,pre{{font-family:ui-monospace,monospace;background:#f4f6f8;border-radius:6px}}
code{{padding:2px 5px}}pre{{padding:16px;overflow:auto;white-space:pre-wrap}}
.card{{border:1px solid #dce1e7;border-radius:12px;padding:20px;margin:18px 0}}
h1{{font-size:25px}}h2{{font-size:18px;margin-top:26px}}.ok{{color:#177245}}
</style></head><body>
<h1>{title}</h1>
<p class="ok">这是一个可用的 Agent Forge MCP 发布物。</p>
<div class="card"><b>MCP 地址</b><pre>{url}</pre>
<b>传输协议</b>：Streamable HTTP<br><b>可见性</b>：{mode}<br>
<b>工具</b>：<code>run_task</code>、<code>get_run</code></div>
<h2>如何使用</h2>
<p>这个地址不是普通网页 API。MCP 客户端需要向同一地址发送
<code>POST</code> JSON-RPC 请求。网页登录后请回到发布页点击“复制 MCP 配置”，系统会自动签发当前账号专属 API Key，无需手工编码或填写账号密码。</p>
<p>旧客户端仍可使用 HTTP Basic 认证，但新配置建议使用发布页生成的 Bearer 凭证。</p>
<p>Bearer 凭证仅在创建配置时显示一次，请像密码一样妥善保管；可通过账号凭证管理 API 统一吊销。</p>
<h2>调用流程</h2>
<ol><li>调用 <code>run_task</code>，传入 <code>task</code>。</li>
<li>保存返回的 <code>run_id</code>。</li>
<li>调用 <code>get_run</code> 查询团队过程、状态和最终结果。</li></ol>
<p>原始 thinking/隐式思维链不会公开；返回的是可验证的任务拆解、
团队调度、工具调用、成员产出与最终结果。</p>
</body></html>"""
    return Response(content=page, media_type="text/html")


@agent_share_mcp_router.post("/agent-share/mcp/{published_id}")
async def published_agent_mcp(published_id: str, request: Request) -> Response:
    caller = _caller(request)
    publication = await _publication(request.app.state.storage, caller, published_id)
    if publication is None:
        return JSONResponse({"detail": "发布物不存在或无权访问"}, status_code=404)
    meta, _share = publication
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(None, -32700, "JSON 解析失败")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _error(body.get("id") if isinstance(body, dict) else None, -32600, "无效请求")
    method = body.get("method")
    request_id = body.get("id")
    if request_id is None:
        return Response(status_code=202)
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Agent Forge Publication MCP", "version": "1.0.0"},
        })
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})
    if method != "tools/call":
        return _error(request_id, -32601, f"未知方法: {method}")
    params = body.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _error(request_id, -32602, "arguments 必须是对象")
    try:
        if name == "run_task":
            payload = await _run_task(request.app.state.storage, caller, published_id, args)
        elif name == "get_run":
            payload = await _get_run(
                request.app.state.storage, caller, published_id, args, meta,
            )
        else:
            return _error(request_id, -32602, f"未知工具: {name}")
    except ValueError as exc:
        return _result(request_id, _tool_result(str(exc), error=True))
    return _result(request_id, _tool_result(payload))
