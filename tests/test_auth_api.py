"""auth API + AuthMiddleware 集成测试：登录/登出/鉴权拦截/身份注入全链路。

覆盖安全要点：
- 未登录：API 401、页面 307 跳 /login、静态资源放行
- 登录：正确/错误凭据、cookie 属性（httponly/samesite/path）
- 身份注入：客户端伪造 X-User-ID 必须被服务端会话覆盖
- 会话：登出即失效、会话 TTL 滑动续期、多用户数据隔离
"""

import pytest

from app import auth as auth_mod

from tests.helpers import login


class TestLoginApi:
    def test_login_success_sets_cookie(self, client):
        resp = login(client)
        assert resp.status_code == 200
        assert resp.json() == {"username": "alice"}
        cookie = resp.cookies.get(auth_mod.SESSION_COOKIE)
        assert cookie, "登录必须下发会话 cookie"

    def test_cookie_security_attributes(self, client):
        resp = login(client)
        set_cookie = resp.headers["set-cookie"]
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()
        assert f"path=/" in set_cookie.lower()

    def test_login_wrong_password_401(self, client):
        resp = login(client, password="wrong")
        assert resp.status_code == 401
        assert "用户名或密码错误" in resp.json()["detail"]

    def test_login_unknown_user_401(self, client):
        resp = login(client, username="ghost")
        assert resp.status_code == 401

    def test_login_empty_body_422(self, client):
        resp = client.post("/auth/login", json={})
        assert resp.status_code == 422

    def test_me_requires_login(self, client):
        assert client.get("/auth/me").status_code == 401

    def test_me_after_login(self, client):
        login(client)
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json() == {"username": "alice"}


class TestMiddlewareAccess:
    """AuthMiddleware 的放行/拦截矩阵。"""

    def test_api_without_login_401(self, client):
        resp = client.get("/echo")
        assert resp.status_code == 401
        assert "未登录" in resp.json()["detail"]

    def test_post_without_login_401(self, client):
        assert client.post("/echo").status_code == 401

    def test_page_without_login_redirects(self, client):
        resp = client.get("/", follow_redirects=False, headers={"Accept": "text/html"})
        assert resp.status_code == 307
        assert resp.headers["location"] == "/login"

    def test_index_html_redirects(self, client):
        resp = client.get(
            "/index.html", follow_redirects=False, headers={"Accept": "text/html"}
        )
        assert resp.status_code == 307

    def test_deep_link_pages_redirect_for_browsers(self, client):
        """未登录浏览器直接打开/刷新深链接（/chat、/mcp、/skill 等）
        也必须 307 → /login，而不是裸 401 JSON（2026-09-04 E2E 修复）。"""
        for path in ("/chat", "/mcp", "/skill", "/credential", "/knowledge"):
            resp = client.get(
                path, follow_redirects=False, headers={"Accept": "text/html"}
            )
            assert resp.status_code == 307, f"{path} 应 307 跳登录页"
            assert resp.headers["location"] == "/login"

    def test_deep_link_401_for_api_clients(self, client):
        """API 客户端（Accept: */* 或 application/json）保持 401 JSON。"""
        for path in ("/chat", "/mcp"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 401
            assert "未登录" in resp.json()["detail"]

    def test_login_page_public(self, client):
        assert client.get("/login").status_code == 200

    def test_auth_endpoints_public(self, client):
        # /auth/* 全放行（login/me 自行处理未登录态）
        assert client.get("/auth/me").status_code == 401  # 业务 401 而非中间件拦截

    def test_health_public(self, client):
        # /health 在 STATIC_EXACT 中（生产用于探活）
        assert client.get("/health").status_code in (200, 404)

    def test_static_assets_public(self, client):
        # /assets/* 前缀放行（登录页引用的 js/css）
        assert client.get("/assets/app.js").status_code in (200, 404)


class TestIdentityInjection:
    """安全核心：X-User-ID 以服务端会话为准。"""

    def test_forged_header_overwritten(self, client):
        login(client)
        resp = client.get("/echo", headers={"X-User-ID": "mallory"})
        assert resp.status_code == 200
        assert resp.json()["x_user_id"] == "alice"

    def test_forged_header_without_login_rejected(self, client):
        # 只有伪造头、无会话 cookie → 401
        resp = client.get("/echo", headers={"X-User-ID": "mallory"})
        assert resp.status_code == 401

    def test_normal_request_carries_identity(self, client):
        login(client)
        assert client.get("/echo").json()["x_user_id"] == "alice"

    def test_garbage_cookie_rejected(self, client):
        client.cookies.set(auth_mod.SESSION_COOKIE, "not-a-real-token")
        assert client.get("/echo").status_code == 401


class TestLogoutAndSession:
    def test_logout_invalidates_session(self, client):
        login(client)
        assert client.get("/echo").status_code == 200
        resp = client.post("/auth/logout")
        assert resp.status_code == 200
        assert client.get("/echo").status_code == 401

    def test_session_shared_across_requests(self, client):
        login(client)
        for _ in range(3):
            assert client.get("/echo").json()["x_user_id"] == "alice"

    async def test_session_ttl_sliding_renewal(self, fake_redis, client):
        login(client)
        token = client.cookies.get(auth_mod.SESSION_COOKIE)
        key = auth_mod.SESS_PREFIX + token
        await fake_redis.expire(key, 10)  # 模拟即将过期
        assert client.get("/echo").status_code == 200  # 触发滑动续期
        ttl = await fake_redis.ttl(key)
        assert ttl > 10, "活跃会话应被续期回完整 TTL"

    async def test_expired_session_rejected(self, fake_redis, client):
        login(client)
        token = client.cookies.get(auth_mod.SESSION_COOKIE)
        await fake_redis.delete(auth_mod.SESS_PREFIX + token)
        assert client.get("/echo").status_code == 401


class TestMultiUserIsolation:
    def test_two_users_separate_identities(self, users_dir, client):
        (users_dir / "bob.txt").write_text("bob-pw-456", encoding="utf-8")
        login(client)
        assert client.get("/echo").json()["x_user_id"] == "alice"

        # bob 登录（同一 client，cookie 被替换）
        resp = login(client, username="bob", password="bob-pw-456")
        assert resp.status_code == 200
        assert client.get("/echo").json()["x_user_id"] == "bob"

    async def test_sessions_are_independent(self, fake_redis, users_dir):
        (users_dir / "bob.txt").write_text("bob-pw-456", encoding="utf-8")
        t1 = await auth_mod._new_session("alice")
        t2 = await auth_mod._new_session("bob")
        assert await fake_redis.get(auth_mod.SESS_PREFIX + t1) == "alice"
        assert await fake_redis.get(auth_mod.SESS_PREFIX + t2) == "bob"


class TestPasswordFileHotReload:
    def test_password_change_takes_effect_next_login(self, users_dir, client):
        login(client)
        assert client.get("/echo").status_code == 200
        # 改密码文件 → 旧密码失效、新密码可用
        (users_dir / "alice.txt").write_text("new-pw-789", encoding="utf-8")
        assert login(client).status_code == 401
        resp = login(client, password="new-pw-789")
        assert resp.status_code == 200

    def test_remove_user_blocks_login(self, users_dir, client):
        (users_dir / "alice.txt").unlink()
        assert login(client).status_code == 401


@pytest.mark.parametrize("path,method", [
    ("/agents", "GET"), ("/agent/", "GET"), ("/credential/", "GET"),
    ("/sessions/", "POST"), ("/chat/", "POST"), ("/knowledge_bases/", "GET"),
])
def test_production_paths_blocked_without_login(client, path, method):
    """生产路由抽查：未登录一律 401（无一是公开业务端点）。"""
    resp = client.request(method, path)
    assert resp.status_code == 401
