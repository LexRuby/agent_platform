"""Workspace tools that keep host paths outside the agent-visible contract."""

from __future__ import annotations

import copy
import os
import re
import shlex
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk, ToolMiddlewareBase
from agentscope.tool._builtin import Bash, Edit, Glob, Grep, Read, Write
from agentscope.tool._builtin._backend import ExecResult, LocalBackend


RELATIVE_PATH_DESCRIPTION = (
    "Path relative to the current workspace. Use '.' for its root. "
    "Never use or reveal the physical server location."
)


class WorkspaceLocalBackend(LocalBackend):
    """Local backend with direct, bounded filesystem operations."""

    async def read_file(self, path: str) -> bytes:
        with open(path, "rb") as file:
            return file.read()

    async def write_file(self, path: str, data: bytes) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as file:
            file.write(data)

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ):
        if len(command) >= 3 and command[:2] == ["mkdir", "-p"]:
            for path in command[2:]:
                target = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
                os.makedirs(target, exist_ok=True)
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        return await super().exec_shell(command, cwd=cwd, timeout=timeout)


def _schema_with_relative_path(schema: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(schema)
    properties = updated.get("properties", {})
    for key in ("file_path", "path"):
        if key in properties:
            properties[key]["description"] = RELATIVE_PATH_DESCRIPTION
    return updated


def _relative_target(root: str, value: str | None) -> str:
    raw = (value or ".").strip()
    if not raw or "\x00" in raw:
        raise ValueError("路径必须是当前工作区内的相对路径")
    real_root = os.path.realpath(root)
    if os.path.isabs(raw):
        legacy_target = os.path.realpath(raw)
        try:
            inside = os.path.commonpath([real_root, legacy_target]) == real_root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("路径必须是当前工作区内的相对路径")
        raw = os.path.relpath(legacy_target, real_root)
    normalized = os.path.normpath(raw)
    if normalized == ".." or normalized.startswith(f"..{os.sep}"):
        raise ValueError("路径不能离开当前工作区")
    candidate = os.path.realpath(os.path.join(real_root, normalized))
    try:
        inside = os.path.commonpath([real_root, candidate]) == real_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("路径不能离开当前工作区")
    return candidate


def _sanitize_text(value: str, root: str) -> str:
    clean_root = os.path.realpath(root).rstrip(os.sep)
    return value.replace(clean_root, ".")


def _sanitize_value(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, root)
    if isinstance(value, dict):
        return {key: _sanitize_value(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, root) for item in value]
    return value


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


def _unsafe_shell_path(command: str, root: str) -> bool:
    command = command.replace(os.path.realpath(root), ".")
    if re.search(r"(^|[\s<>=|;&(`])/(?!/)", command):
        return True
    for token in _shell_tokens(command):
        candidate = token.strip("'\"(){}[],:;").lstrip("<>")
        if "=" in candidate:
            candidate = candidate.split("=", 1)[1]
        if candidate.startswith("/"):
            return True
        if candidate == ".." or candidate.startswith("../"):
            return True
        if "/../" in candidate or candidate.endswith("/.."):
            return True
    return False


class WorkspacePathMiddleware(ToolMiddlewareBase):
    """Resolve relative inputs internally and redact physical paths in output."""

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        updated = dict(input_kwargs)
        try:
            if tool.name in {"Read", "Write", "Edit"}:
                updated["file_path"] = _relative_target(
                    self.root, updated.get("file_path"),
                )
            elif tool.name in {"Glob", "Grep"}:
                updated["path"] = _relative_target(
                    self.root, updated.get("path") or ".",
                )
            elif tool.name == "Bash":
                command = str(updated.get("command", ""))
                command = command.replace(self.root, ".")
                if _unsafe_shell_path(command, self.root):
                    raise ValueError("命令只能访问当前工作区内的相对路径")
                updated["command"] = command
        except ValueError as exc:
            yield ToolChunk(
                content=[TextBlock(text=str(exc))],
                state=ToolResultState.DENIED,
                is_last=True,
            )
            return

        async for chunk in next_handler(**updated):
            safe = chunk.model_copy(deep=True)
            for block in safe.content:
                if isinstance(block, TextBlock):
                    block.text = _sanitize_text(block.text, self.root)
            safe.metadata = _sanitize_value(safe.metadata, self.root)
            yield safe


class _WorkspaceToolMixin:
    workspace_root: str

    def _set_workspace_root(self, root: str) -> None:
        self.workspace_root = os.path.realpath(root)

    def _path_in_allowed_working_path(
        self,
        file_path: str,
        context: PermissionContext,
    ) -> bool:
        try:
            _relative_target(self.workspace_root, file_path)
            return True
        except ValueError:
            return False

    def _path_is_safe(self, tool_input: dict[str, Any]) -> bool:
        key = "file_path" if "file_path" in tool_input else "path"
        try:
            _relative_target(self.workspace_root, tool_input.get(key) or ".")
            return True
        except ValueError:
            return False

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        if not self._path_is_safe(tool_input):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="只能访问当前工作区内的相对路径",
                decision_reason="Workspace boundary",
            )
        return await super().check_permissions(tool_input, context)

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        if not self._path_is_safe(tool_input):
            return False
        return await super().check_read_only(tool_input)


class WorkspaceRead(_WorkspaceToolMixin, Read):
    _DESCRIPTION_HEAD = """Reads a file from the current workspace.

Usage:
- Use only a path relative to the workspace root.
- Use '.' for the workspace root.
- Never request, infer, or reveal the physical server location.
- Results include line numbers for plain text files."""
    input_schema = _schema_with_relative_path(Read.input_schema)


class WorkspaceWrite(_WorkspaceToolMixin, Write):
    description = Write.description + "\n- file_path must be workspace-relative."
    input_schema = _schema_with_relative_path(Write.input_schema)


class WorkspaceEdit(_WorkspaceToolMixin, Edit):
    description = Edit.description + "\n- file_path must be workspace-relative."
    input_schema = _schema_with_relative_path(Edit.input_schema)


class WorkspaceGlob(_WorkspaceToolMixin, Glob):
    description = Glob.description + "\n- path must be workspace-relative."
    input_schema = _schema_with_relative_path(Glob.input_schema)


class WorkspaceGrep(_WorkspaceToolMixin, Grep):
    description = Grep.description + "\n- path must be workspace-relative."
    input_schema = _schema_with_relative_path(Grep.input_schema)


class WorkspaceBash(Bash):
    description = re.sub(
        r"Try to maintain your current working directory.*?requests it\.",
        "Use workspace-relative paths. Never run pwd or inspect host paths.",
        Bash.description,
        flags=re.DOTALL,
    )

    def __init__(self, *, workspace_root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.workspace_root = os.path.realpath(workspace_root)

    def _normalized_input(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        updated = dict(tool_input)
        updated["command"] = str(updated.get("command", "")).replace(
            self.workspace_root, ".",
        )
        return updated

    def _path_in_allowed_working_path(
        self,
        file_path: str,
        context: PermissionContext,
    ) -> bool:
        try:
            _relative_target(self.workspace_root, file_path)
            return True
        except ValueError:
            return False

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        normalized = self._normalized_input(tool_input)
        if _unsafe_shell_path(normalized["command"], self.workspace_root):
            return False
        return await super().check_read_only(normalized)

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        normalized = self._normalized_input(tool_input)
        if _unsafe_shell_path(normalized["command"], self.workspace_root):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="命令只能访问当前工作区内的相对路径",
                decision_reason="Workspace boundary",
            )
        return await super().check_permissions(normalized, context)


def build_workspace_tools(root: str, backend: Any) -> list[ToolBase]:
    middleware = [WorkspacePathMiddleware(root)]
    tools: list[ToolBase] = [
        WorkspaceBash(
            workspace_root=root,
            cwd=root,
            backend=backend,
            middlewares=middleware,
        ),
        WorkspaceEdit(backend=backend, middlewares=middleware),
        WorkspaceGlob(backend=backend, middlewares=middleware),
        WorkspaceGrep(backend=backend, middlewares=middleware),
        WorkspaceRead(backend=backend, middlewares=middleware),
        WorkspaceWrite(backend=backend, middlewares=middleware),
    ]
    for tool in tools:
        if isinstance(tool, _WorkspaceToolMixin):
            tool._set_workspace_root(root)
    return tools
