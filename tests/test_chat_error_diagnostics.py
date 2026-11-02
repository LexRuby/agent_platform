import logging
from pathlib import Path

import pytest

from app import chat_error_diagnostics as diagnostics


_BASE_DIR = Path(__file__).resolve().parent.parent


def test_safe_error_text_redacts_common_credentials():
    error = RuntimeError(
        "api_key=secret-one Authorization: secret-two Bearer secret-three",
    )

    text = diagnostics._safe_error_text(error)

    assert "secret-one" not in text
    assert "secret-two" not in text
    assert "secret-three" not in text
    assert text.count("[REDACTED]") == 3


@pytest.mark.asyncio
async def test_patch_logs_setup_context_and_calls_original(monkeypatch, caplog):
    from agentscope.app._service import _chat

    calls = []

    async def original(self, user_id, session_id, agent_id, error, *args, **kwargs):
        calls.append((self, user_id, session_id, agent_id, error, args, kwargs))

    monkeypatch.setattr(_chat.ChatService, "_report_failure", original)
    diagnostics.patch_chat_setup_diagnostics()
    service = object()
    error = RuntimeError("token=must-not-leak")

    with caplog.at_level(logging.ERROR, logger="agentforge.chat"):
        await _chat.ChatService._report_failure(
            service,
            "alice",
            "session-1",
            "agent-1",
            error,
        )

    assert len(calls) == 1
    assert "alice" in caplog.text
    assert "session-1" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "must-not-leak" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_production_entrypoint_does_not_import_itself_twice():
    source = (_BASE_DIR / "agent_service_app.py").read_text(encoding="utf-8")

    assert "uvicorn.run(app," in source
    assert 'uvicorn.run("agent_service_app:app"' not in source
