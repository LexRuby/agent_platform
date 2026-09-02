"""共享 fixture：临时用户目录、fakeredis、受保护的测试用 ASGI 应用。

测试隔离原则：
- 用户目录 → tmp_path（不碰 data/users/ 真实账号）
- Redis → fakeredis（不依赖真实 Redis 服务）
- HTTP → starlette TestClient（ASGI 进程内调用，不占端口）
"""

import fakeredis
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.testclient import TestClient

from app import auth as auth_mod
from app.auth import AuthMiddleware, _LOGIN_HTML, auth_router

from tests.helpers import PASSWORD


@pytest.fixture
def fake_redis(monkeypatch):
    """auth 模块的全局 Redis 池替换为 fakeredis（async 接口）。"""
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(auth_mod, "_pool", r)
    return r


@pytest.fixture
def users_dir(tmp_path, monkeypatch):
    """临时用户文件夹，预置一个 alice 账号。"""
    d = tmp_path / "users"
    d.mkdir()
    (d / "alice.txt").write_text(PASSWORD + "\n", encoding="utf-8")
    monkeypatch.setattr(auth_mod, "USERS_DIR", d)
    return d


@pytest.fixture
def protected_app(fake_redis, users_dir):
    """复刻生产形态 B 拓扑的最小 ASGI 应用：登录页 + 受保护业务端点。"""
    app = FastAPI()
    app.include_router(auth_router)

    @app.get("/login", include_in_schema=False)
    async def login_page() -> HTMLResponse:
        return HTMLResponse(_LOGIN_HTML)

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request):
        # 返回 ASGI 层（AuthMiddleware 注入后）实际收到的 X-User-ID，
        # 用于验证客户端伪造的头被服务端会话身份覆盖
        return {"x_user_id": request.headers.get("x-user-id")}

    return AuthMiddleware(app)


@pytest.fixture
def client(protected_app):
    return TestClient(protected_app)
