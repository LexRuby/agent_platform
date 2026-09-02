"""ASGI lifespan 启动钩子。

create_app 传了自定义 lifespan，FastAPI 的 on_event("startup") 会被忽略，
故从 ASGI 层拦截 lifespan.startup.complete 事件来启动 ARK 模型心跳。
独立成模块便于单测（不触发 agent_service_app 的 create_app 依赖链）。
"""


class StartupHook:
    """包装 ASGI 应用：lifespan startup 完成后调用回调（仅一次）。"""

    def __init__(self, inner, on_startup=None) -> None:
        self.inner = inner
        self._started = False
        self.on_startup = on_startup

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "lifespan":
            await self.inner(scope, receive, send)
            return

        async def send_wrapper(message) -> None:
            if (
                message["type"] == "lifespan.startup.complete"
                and not self._started
            ):
                self._started = True
                if self.on_startup is not None:
                    self.on_startup()
            await send(message)

        await self.inner(scope, receive, send_wrapper)


def _default_on_startup() -> None:
    from app.ark_credential import start_heartbeat

    start_heartbeat()
