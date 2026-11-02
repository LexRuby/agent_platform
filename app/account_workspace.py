"""Account-scoped conversation and project workspaces.

The upstream local manager stores every workspace for an agent in the same
directory.  AgentForge instead gives each conversation its own disposable
space while allowing an explicitly selected project workspace to be shared
and retained.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time

from agentscope.app.workspace_manager import IsolationPolicy, LocalWorkspaceManager
from agentscope.workspace import LocalWorkspace


PROJECT_WORKSPACE_PREFIX = "project_"


def _safe_component(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest()


class AccountWorkspaceManager(LocalWorkspaceManager):
    """Local workspaces isolated by account and workspace binding."""

    def __init__(self, basedir: str, **kwargs) -> None:
        super().__init__(
            basedir,
            isolation=IsolationPolicy.PER_SESSION,
            **kwargs,
        )

    def workdir_for(self, user_id: str, workspace_id: str) -> str:
        kind = (
            "projects"
            if workspace_id.startswith(PROJECT_WORKSPACE_PREFIX)
            else "conversations"
        )
        path = os.path.join(
            self._basedir,
            _safe_component(user_id),
            kind,
            _safe_component(workspace_id),
        )
        root = os.path.realpath(self._basedir)
        resolved = os.path.realpath(path)
        if os.path.commonpath([root, resolved]) != root:
            raise PermissionError("Workspace path escapes its account root.")
        return path

    @staticmethod
    def _cache_key(user_id: str, workspace_id: str) -> str:
        return f"{_safe_component(user_id)}:{workspace_id}"

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> LocalWorkspace:
        if not workspace_id:
            workspace_id = await self.assign_workspace_id(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )
        cache_key = self._cache_key(user_id, workspace_id)

        async with self._lock:
            now = time.monotonic()
            expired = self._pop_expired(now)
            cached = self._cache.get(cache_key)
            if cached is not None:
                workspace, _ = cached
                self._cache[cache_key] = (workspace, now)
                hit = workspace
            else:
                hit = None

        if expired:
            await asyncio.gather(
                *(self._safe_close(workspace) for workspace in expired),
                return_exceptions=True,
            )
        if hit is not None:
            return hit

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                workspace, _ = cached
                self._cache[cache_key] = (workspace, time.monotonic())
                return workspace

            workdir = self.workdir_for(user_id, workspace_id)
            await self._migrate_owned_legacy_workspace(
                user_id=user_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                destination=workdir,
            )
            os.makedirs(workdir, mode=0o700, exist_ok=True)
            os.chmod(workdir, 0o700)
            workspace = LocalWorkspace(
                workspace_id=workspace_id,
                workdir=workdir,
                default_mcps=self._default_mcps,
                skill_paths=self._skill_paths,
            )
            await workspace.initialize()
            self._cache[cache_key] = (workspace, time.monotonic())
            return workspace

    async def _migrate_owned_legacy_workspace(
        self,
        *,
        user_id: str,
        agent_id: str,
        workspace_id: str,
        destination: str,
    ) -> bool:
        """Move a provably owned pre-isolation agent directory once.

        Old local workspaces were stored at ``basedir/agent_id``. We only
        migrate an agent that belongs to this account, and bind its files to
        the oldest remaining session. This prevents a newly-created chat from
        taking over legacy files and avoids copying a shared agent owner's
        files into a viewer account.
        """
        if os.path.exists(destination) or self._storage is None:
            return False
        owned_agent = await self._storage.get_agent(user_id, agent_id)
        if owned_agent is None:
            return False
        sessions = await self._storage.list_sessions(user_id, agent_id)
        if not sessions or sessions[-1].config.workspace_id != workspace_id:
            return False
        legacy = os.path.realpath(os.path.join(self._basedir, agent_id))
        root = os.path.realpath(self._basedir)
        if os.path.commonpath([root, legacy]) != root or not os.path.isdir(legacy):
            return False
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        shutil.move(legacy, destination)
        return True

    async def close_for_user(self, user_id: str, workspace_id: str) -> None:
        async with self._lock:
            entry = self._cache.pop(self._cache_key(user_id, workspace_id), None)
        if entry is not None:
            await self._safe_close(entry[0])

    async def delete_temporary_workspace(
        self,
        user_id: str,
        workspace_id: str,
    ) -> bool:
        """Remove one conversation workspace; project spaces are retained."""
        if not workspace_id or workspace_id.startswith(PROJECT_WORKSPACE_PREFIX):
            return False
        await self.close_for_user(user_id, workspace_id)
        workdir = self.workdir_for(user_id, workspace_id)
        if not os.path.isdir(workdir):
            return False
        # Conversation spaces are bounded application directories. Removing
        # them synchronously also avoids depending on the process-wide thread
        # executor during session deletion and service shutdown.
        shutil.rmtree(workdir)
        return True
