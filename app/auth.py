"""用户管理：文件驱动的登录/登出 + Redis 会话 + ASGI 鉴权中间件。

设计：
- 用户来源：不开放注册。服务器上的用户文件夹（AGENTFORGE_USERS_DIR，默认
  ``data/users/``）里每个 ``<用户名>.txt`` 文件即一个账号，文件内容为明文密码
  （首尾空白忽略）。增删用户/改密码 = 增删改文件，下次登录生效。
- 会话：随机 token 存 Redis ``agentforge:sess:<token>`` → username，TTL 7 天（滑动续期）
- 鉴权：ASGI 中间件校验 cookie ``agentforge_session``，通过后把身份重写进
  ``X-User-ID`` 请求头——客户端伪造的 X-User-ID 无效；未登录时业务 API 返回
  401，页面入口 302 跳转 /login
- 官方 Web UI 前端零改动：同源部署浏览器自动携带 cookie
"""

import json
import os
import re
import secrets
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

REDIS_HOST = os.environ.get("AGENTFORGE_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("AGENTFORGE_REDIS_PORT", "6379"))
USERS_DIR = Path(os.environ.get("AGENTFORGE_USERS_DIR", "data/users"))

SESSION_COOKIE = "agentforge_session"
SESSION_TTL = 7 * 24 * 3600
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")

SESS_PREFIX = "agentforge:sess:"

# 放行规则：认证端点、登录页与静态资源（未登录可访问）
STATIC_EXACT = {"/login", "/health", "/agentscope.svg", "/favicon.ico"}
STATIC_PREFIX = ("/assets/",)

_pool: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return _pool


def _check_password(username: str, password: str) -> bool:
    """对照用户文件夹里的明文密码文件校验。"""
    if not USERNAME_RE.match(username):
        return False
    pw_file = USERS_DIR / f"{username}.txt"
    try:
        stored = pw_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(stored) and secrets.compare_digest(password, stored)


async def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    await _redis().set(SESS_PREFIX + token, username, ex=SESSION_TTL)
    return token


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
    )


class AuthBody(BaseModel):
    username: str
    password: str


auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login", summary="登录（用户由服务器 users 文件夹维护，无注册）")
async def login(body: AuthBody, response: Response) -> dict:
    if not _check_password(body.username, body.password):
        raise HTTPException(401, "用户名或密码错误")
    token = await _new_session(body.username)
    _set_session_cookie(response, token)
    return {"username": body.username}


@auth_router.post("/logout", summary="登出")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@auth_router.get("/me", summary="当前登录用户（未登录返回 401）")
async def me(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "未登录")
    username = await _redis().get(SESS_PREFIX + token)
    if username is None:
        raise HTTPException(401, "未登录或会话已过期")
    return {"username": username}


_LOGIN_HTML = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")


class AuthMiddleware:
    """ASGI 鉴权中间件：cookie 会话校验 + 身份注入。

    放行：/auth/*、GET 静态资源（/login、/assets/*、图标等）。
    拦截：其余一切业务请求——未登录 API 返回 401、页面入口 302 /login；
    已登录则把会话身份写入 X-User-ID 后放行（覆盖客户端可能伪造的值）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope["method"]

        if path.startswith("/auth/"):
            await self.app(scope, receive, send)
            return
        if method == "GET" and (
            path in STATIC_EXACT or path.startswith(STATIC_PREFIX)
        ):
            await self.app(scope, receive, send)
            return

        username = await self._session_user(scope)
        if username is None:
            if method == "GET" and path in ("/", "/index.html"):
                await _send_redirect(send, "/login")
            else:
                await _send_unauthorized(send)
            return

        # 身份以服务端会话为准，覆盖任何客户端提供的 X-User-ID
        headers = [
            (k, v) for k, v in scope["headers"] if k != b"x-user-id"
        ]
        headers.append((b"x-user-id", username.encode()))
        scope["headers"] = headers
        await self.app(scope, receive, send)

    async def _session_user(self, scope) -> str | None:
        cookies = {}
        for key, value in scope.get("headers", []):
            if key == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    name, _, val = part.strip().partition("=")
                    cookies[name] = val
        token = cookies.get(SESSION_COOKIE)
        if not token:
            return None
        r = _redis()
        username = await r.get(SESS_PREFIX + token)
        if username is None:
            return None
        # 滑动续期
        await r.expire(SESS_PREFIX + token, SESSION_TTL)
        return username


async def _send_unauthorized(send) -> None:
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json; charset=utf-8")],
    })
    await send({
        "type": "http.response.body",
        "body": json.dumps(
            {"detail": "未登录或会话已过期，请访问 /login"},
            ensure_ascii=False,
        ).encode(),
    })


async def _send_redirect(send, location: str) -> None:
    await send({
        "type": "http.response.start",
        "status": 307,
        "headers": [(b"location", location.encode())],
    })
    await send({"type": "http.response.body", "body": b""})
