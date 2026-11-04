"""AgentForge defaults for session file permissions and cleanup."""

from __future__ import annotations

import logging
from typing import Any

from agentscope.permission import PermissionMode
from agentscope.state import AgentState

from app.account_workspace import AccountWorkspaceManager


logger = logging.getLogger(__name__)
_upsert_orig = None
_delete_orig = None


def patch_session_defaults() -> None:
    """Auto-authorize current-space edits and clean deleted chat files."""
    from agentscope.app._service._session import SessionService
    from agentscope.app.storage._redis_storage import RedisStorage

    global _upsert_orig, _delete_orig

    if not getattr(RedisStorage.upsert_session, "_agentforge_defaults", False):
        _upsert_orig = RedisStorage.upsert_session

        async def upsert_with_defaults(
            self: Any,
            user_id: str,
            agent_id: str,
            config: Any,
            state: AgentState | None = None,
            session_id: str | None = None,
            **kwargs: Any,
        ):
            # A caller-provided state is authoritative. Only brand-new normal
            # sessions receive the interactive workspace-editing default.
            if state is None and session_id is None:
                state = _new_session_state()
            return await _upsert_orig(
                self,
                user_id,
                agent_id,
                config,
                state=state,
                session_id=session_id,
                **kwargs,
            )

        upsert_with_defaults._agentforge_defaults = True
        RedisStorage.upsert_session = upsert_with_defaults

    if not getattr(SessionService.delete_session, "_agentforge_cleanup", False):
        _delete_orig = SessionService.delete_session

        async def delete_with_workspace_cleanup(
            self: Any,
            user_id: str,
            agent_id: str,
            session_id: str,
        ) -> bool:
            record = await self._storage.get_session(user_id, agent_id, session_id)
            workspace_id = record.config.workspace_id if record else None
            deleted = await _delete_orig(self, user_id, agent_id, session_id)
            manager = self._workspace_manager
            if deleted and workspace_id and isinstance(manager, AccountWorkspaceManager):
                await _cleanup_temporary_workspace(
                    manager, self._storage, user_id, workspace_id,
                )
            return deleted

        delete_with_workspace_cleanup._agentforge_cleanup = True
        SessionService.delete_session = delete_with_workspace_cleanup


async def _workspace_is_referenced(storage: Any, user_id: str, workspace_id: str) -> bool:
    """Return whether any remaining session owned by the account uses it."""
    agent_ids = {agent.id for agent in await storage.list_agents(user_id)}
    # Team-created agents are intentionally absent from list_agents(). Their
    # sessions can still share the leader's workspace, so include team members.
    for team in await storage.list_teams(user_id):
        agent_ids.update(team.member_ids)
    for agent_id in agent_ids:
        for session in await storage.list_sessions(user_id, agent_id):
            if session.config.workspace_id == workspace_id:
                return True
    return False


async def _cleanup_temporary_workspace(
    manager: AccountWorkspaceManager,
    storage: Any,
    user_id: str,
    workspace_id: str,
) -> None:
    """Best-effort cleanup after a session delete has already committed."""
    try:
        referenced = await _workspace_is_referenced(storage, user_id, workspace_id)
    except Exception:
        logger.exception(
            "Failed to inspect references for temporary workspace %s", workspace_id,
        )
        return
    if not referenced:
        try:
            await manager.delete_temporary_workspace(user_id, workspace_id)
        except Exception:
            logger.exception("Failed to delete temporary workspace %s", workspace_id)


def _new_session_state() -> AgentState:
    state = AgentState()
    state.permission_context.mode = PermissionMode.ACCEPT_EDITS
    return state
