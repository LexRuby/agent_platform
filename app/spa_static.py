"""SPA 静态文件服务：未知路径回退 index.html。

官方 Web UI 是 React 单页应用，``/chat/<agent>/<session>`` 这类路径是
前端路由。浏览器直接刷新深链接时，请求会打到服务端；静态目录里没有
对应文件，Starlette 会抛 ``HTTPException(404)``。本类在文件未命中时
回退 ``index.html``，由前端路由接管渲染。

仅当请求头 ``Accept`` 含 ``text/html``（浏览器导航）时回退——API
客户端（``application/json``）的 404 保持原样，不会被 HTML 掩盖。
"""

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class SPAStaticFiles(StaticFiles):
    """带 SPA fallback 的静态文件服务。"""

    async def get_response(self, path: str, scope) -> Response:
        try:
            response = await super().get_response(path, scope)
            # 访问 "/" 时 StaticFiles(html=True) 自动返回 index.html，
            # 与深链接回退同样需要 no-cache（旧 HTML 引用已删除的
            # 旧 hash chunk → 页面瘫痪，见类 docstring）
            if response.path.endswith("index.html"):
                response.headers["Cache-Control"] = "no-cache"
            return response
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        # 仅浏览器导航回退到 SPA 入口；API 请求保持 404
        headers = scope.get("headers") or []
        accept = next(
            (
                v.decode("latin-1")
                for k, v in headers
                if k.decode("latin-1").lower() == "accept"
            ),
            "",
        )
        if "text/html" not in accept:
            raise HTTPException(status_code=404)
        response = await super().get_response("index.html", scope)
        # index.html 每次回源校验：它引用的 /assets/*-<hash>.js 在重建后
        # 全部换名，若被浏览器缓存，旧 HTML 会去请求已删除的旧 chunk
        # （404 → 页面 JS 加载失败，所有交互瘫痪，2026-09-03 发送按钮
        # 无法点击事故）。带 hash 的 assets 仍可长缓存。
        response.headers["Cache-Control"] = "no-cache"
        return response


# ---------------------------------------------------------------------------
# 深链接导航回退中间件
# ---------------------------------------------------------------------------

# 与后端 API 路径冲突的前端页面（GET /mcp 等是官方 API，浏览器刷新
# /mcp 页面时会被路由匹配返回 JSON 而不是页面）。2026-09-03 用户反馈：
# 刷新 http://…:30000/mcp 看到 JSON。
SPA_PAGE_PREFIXES = (
    "/chat",
    "/schedule",
    "/channel",
    "/credential",
    "/mcp",
    "/skill",
    "/knowledge",
)


def _is_spa_navigation(scope) -> bool:
    """GET + Accept 含 text/html + 路径是前端页面 → 浏览器导航。"""
    if scope.get("method") != "GET":
        return False
    path = scope.get("path", "")
    for prefix in SPA_PAGE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            break
    else:
        return False
    accept = next(
        (
            v.decode("latin-1")
            for k, v in scope.get("headers") or []
            if k.decode("latin-1").lower() == "accept"
        ),
        "",
    )
    return "text/html" in accept


class SPAPageFallbackMiddleware:
    """浏览器深链接刷新时返回 SPA 入口，即使路径与 API 路由冲突。

    API 客户端（Accept: application/json / */*）不受影响，照常拿到 JSON。
    """

    def __init__(self, app, index_path: str):
        self.app = app
        self.index_path = index_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and _is_spa_navigation(scope):
            from starlette.responses import FileResponse

            response = FileResponse(
                self.index_path,
                headers={"Cache-Control": "no-cache"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
