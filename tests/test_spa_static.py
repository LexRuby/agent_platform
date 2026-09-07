"""spa_static 模块测试：SPA 深链接刷新回退 index.html。

测试隔离原则：静态目录 → tmp_path，ASGI 进程内调用，不占端口。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.spa_static import SPAStaticFiles

# 浏览器导航会带 text/html；API 客户端通常是 application/json
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
JSON_ACCEPT = "application/json"


@pytest.fixture
def web_dir(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body>SPA_ROOT</body></html>", encoding="utf-8",
    )
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text(
        "console.log('app')", encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def client(web_dir):
    app = FastAPI()
    app.mount("/", SPAStaticFiles(directory=str(web_dir), html=True))
    return TestClient(app)


class TestSPAStaticFiles:
    """静态文件正常服务 + 深链接 fallback。"""

    def test_root_serves_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "SPA_ROOT" in r.text

    def test_real_file_served(self, client):
        r = client.get("/assets/app.js")
        assert r.status_code == 200
        assert "console.log" in r.text

    def test_deep_link_fallback_for_browser(self, client):
        # 浏览器刷新 /chat/<agent>/<session> → index.html（前端路由接管）
        r = client.get(
            "/chat/5375ba4c6ba14a98ae4c8f35fca383f5/cec9b936e62341aca81e21e3d7662ec4",
            headers={"Accept": HTML_ACCEPT},
        )
        assert r.status_code == 200
        assert "SPA_ROOT" in r.text

    def test_nested_deep_link_fallback(self, client):
        r = client.get(
            "/settings/credentials/some/sub/path",
            headers={"Accept": HTML_ACCEPT},
        )
        assert r.status_code == 200
        assert "SPA_ROOT" in r.text

    def test_api_404_not_masked_by_html(self, client):
        # API 客户端（非 text/html）访问未知路径 → 保持 404，不返回 HTML
        r = client.get(
            "/not/an/api/path",
            headers={"Accept": JSON_ACCEPT},
        )
        assert r.status_code == 404

    def test_no_accept_header_stays_404(self, client):
        # 无 Accept 头（如某些探测脚本）→ 保持 404
        r = client.get("/not/a/page")
        assert r.status_code == 404

    def test_missing_file_with_html_accept_when_no_index(self, tmp_path):
        # 目录里没有 index.html 时，fallback 自身也 404（不抛异常）
        (tmp_path / "only.txt").write_text("x", encoding="utf-8")
        app = FastAPI()
        app.mount("/", SPAStaticFiles(directory=str(tmp_path), html=True))
        r = TestClient(app).get(
            "/deep/link", headers={"Accept": HTML_ACCEPT},
        )
        assert r.status_code == 404

    def test_directory_traversal_no_leak(self, client):
        # 路径穿越：客户端规范化后是未知路径 → 回退 index.html，
        # 任何情况下都不返回敏感文件内容
        r = client.get(
            "/../etc/passwd", headers={"Accept": HTML_ACCEPT},
        )
        assert r.status_code == 200
        assert "SPA_ROOT" in r.text
        assert "root:" not in r.text  # 未泄漏系统文件内容


class TestConditionalRequests:
    """浏览器条件请求（If-None-Match/If-Modified-Since）回归测试。

    2026-09-07 事故：用户刷新页面时浏览器带缓存校验头，Starlette
    返回 NotModifiedResponse(304)，旧代码 ``response.path`` 直接
    AttributeError → 500 → 前端页面瘫痪、发送按钮不可用。
    """

    def test_root_conditional_get_returns_304_not_500(self, client):
        # 首次请求拿到 ETag，再带 If-None-Match 校验 → 必须 304（曾 500）
        first = client.get("/")
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag, "StaticFiles 应为 index.html 生成 ETag"

        r = client.get("/", headers={"If-None-Match": etag})
        assert r.status_code == 304, (
            f"条件请求应返回 304，实际 {r.status_code}：{r.text[:200]}"
        )

    def test_root_200_carries_no_cache(self, client):
        # 首次 200 响应必须带 no-cache（index.html 引用 hash chunk，
        # 部署后必须回源校验）
        r = client.get("/")
        assert r.status_code == 200
        assert "no-cache" in r.headers.get("cache-control", "")

    def test_deep_link_fallback_conditional_304(self, client):
        # 深链接回退路径同样要经受条件请求考验
        first = client.get("/chat/a/b", headers={"Accept": HTML_ACCEPT})
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag

        r = client.get(
            "/chat/a/b",
            headers={"Accept": HTML_ACCEPT, "If-None-Match": etag},
        )
        assert r.status_code == 304

    def test_asset_conditional_get_304(self, client):
        # 静态资产走同一条 get_response 覆写路径，条件请求同样不能 500
        first = client.get("/assets/app.js")
        assert first.status_code == 200
        etag = first.headers.get("etag")
        assert etag

        r = client.get("/assets/app.js", headers={"If-None-Match": etag})
        assert r.status_code == 304

    def test_stale_etag_returns_fresh_200(self, client):
        # ETag 不匹配（部署后文件已变）→ 返回新 200 而非 304
        r = client.get("/", headers={"If-None-Match": 'W/"stale-etag"'})
        assert r.status_code == 200
        assert "SPA_ROOT" in r.text

    def test_if_modified_since_304(self, client):
        # If-Modified-Since 条件分支（无 ETag 时的校验方式）
        first = client.get("/")
        last_modified = first.headers.get("last-modified")
        assert last_modified

        r = client.get("/", headers={"If-Modified-Since": last_modified})
        assert r.status_code == 304
