"""StartupHook（ASGI lifespan 拦截）测试：心跳在 startup 完成时被启动、仅一次。

纯 ASGI 级测试：不导入 agent_service_app（其模块级 create_app 依赖 Redis），
直接对 app.startup_hook.StartupHook 构造假 ASGI 应用验证协议行为。
"""

from app.startup_hook import StartupHook


class FakeASGI:
    """记录调用序列的假内层应用。"""

    def __init__(self, *, messages_out=None):
        self.calls = []
        self.messages_out = messages_out or []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["type"])
        for m in self.messages_out:
            await send(m)


async def _next_msg(msg):
    return msg


async def _run_lifespan(app, startup_msgs):
    sent = []

    async def send(m):
        sent.append(m)

    fake = FakeASGI(messages_out=startup_msgs)
    wrapped = StartupHook(fake, on_startup=lambda: sent.append("CALLED"))
    await wrapped(
        {"type": "lifespan"}, lambda: _next_msg({"type": "lifespan.startup"}), send,
    )
    return fake, sent


class TestStartupHook:
    async def test_callback_fired_on_startup_complete(self):
        fake, sent = await _run_lifespan(
            None, [{"type": "lifespan.startup.complete"}],
        )
        assert fake.calls == ["lifespan"]
        assert "CALLED" in sent

    async def test_callback_fired_only_once(self):
        # 异常场景：complete 消息重复出现（不得重复启动心跳）
        _, sent = await _run_lifespan(
            None,
            [
                {"type": "lifespan.startup.complete"},
                {"type": "lifespan.startup.complete"},
            ],
        )
        assert sent.count("CALLED") == 1

    async def test_not_fired_on_startup_failure(self):
        _, sent = await _run_lifespan(
            None, [{"type": "lifespan.startup.failed"}],
        )
        assert "CALLED" not in sent

    async def test_not_fired_on_shutdown(self):
        _, sent = await _run_lifespan(
            None,
            [
                {"type": "lifespan.startup.complete"},
                {"type": "lifespan.shutdown.complete"},
            ],
        )
        assert sent.count("CALLED") == 1  # 仅 startup 触发的那次

    async def test_non_lifespan_scope_passthrough(self):
        """普通 HTTP scope 原样透传给内层应用，不注入任何行为。"""
        fake = FakeASGI()
        wrapped = StartupHook(fake, on_startup=lambda: None)

        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request"}

        await wrapped(
            {"type": "http", "path": "/x", "method": "GET"}, receive, send,
        )
        assert fake.calls == ["http"]  # 内层收到原始 scope
        assert sent == []  # 包装层未额外发消息
