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
            return await super().get_response(path, scope)
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
        return await super().get_response("index.html", scope)
