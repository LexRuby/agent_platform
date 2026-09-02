"""测试辅助：登录操作封装与共享常量。"""

from starlette.testclient import TestClient

from app.auth import SESSION_COOKIE

PASSWORD = "secret-pw-123"


def login(client: TestClient, username: str = "alice", password: str = PASSWORD):
    """用给定凭据登录，返回响应（不断言状态，由调用方判断）。"""
    return client.post(
        "/auth/login", json={"username": username, "password": password},
    )


def get_session_cookie(client: TestClient) -> str:
    return client.cookies.get(SESSION_COOKIE)
