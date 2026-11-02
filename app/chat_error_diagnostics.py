"""Make chat setup failures diagnosable without exposing credentials."""

import logging
import re
import traceback
from typing import Any


_logger = logging.getLogger("agentforge.chat")
_PATCH_MARKER = "_agentforge_setup_diagnostics"
_SECRET_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|authorization|token)\s*[:=]\s*|bearer\s+)\S+",
)


def _safe_error_text(error: Exception) -> str:
    """Return a short exception message with common secret forms redacted."""
    text = str(error).replace("\n", " ")[:500]
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", text)


def patch_chat_setup_diagnostics() -> None:
    """Log the swallowed root cause whenever chat assembly fails.

    AgentScope persists a user-facing setup error but intentionally catches
    the original exception.  Without a server-side record every model,
    workspace or tool assembly fault looks identical in the UI.
    """
    from agentscope.app._service import _chat

    original = _chat.ChatService._report_failure
    if getattr(original, _PATCH_MARKER, False):
        return

    async def report_failure(
        self: Any,
        user_id: str,
        session_id: str,
        agent_id: str,
        error: Exception,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        frames = traceback.extract_tb(error.__traceback__)
        location = " -> ".join(
            f"{frame.filename}:{frame.lineno}:{frame.name}"
            for frame in frames[-4:]
        )
        _logger.error(
            "Chat setup failed user=%r agent=%r session=%r type=%s "
            "detail=%r location=%s",
            user_id,
            agent_id,
            session_id,
            type(error).__name__,
            _safe_error_text(error),
            location or "unknown",
        )
        await original(
            self,
            user_id,
            session_id,
            agent_id,
            error,
            *args,
            **kwargs,
        )

    setattr(report_failure, _PATCH_MARKER, True)
    _chat.ChatService._report_failure = report_failure

