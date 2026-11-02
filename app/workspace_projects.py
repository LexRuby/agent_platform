"""Persistent account projects backed by shared workspace bindings."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.account_workspace import PROJECT_WORKSPACE_PREFIX


_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_FILE = _BASE_DIR / "data" / "workspace_projects.json"


class WorkspaceProject(BaseModel):
    id: str
    name: str
    workspace_id: str
    created_at: str


class CreateWorkspaceProject(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class WorkspaceProjectStore:
    """Small single-process registry; workspace files remain on disk."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path(
            os.environ.get("AGENTFORGE_WORKSPACE_PROJECTS_FILE", _DEFAULT_FILE),
        )
        self._lock = threading.Lock()

    def _load(self) -> dict[str, list[dict]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self, value: dict[str, list[dict]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def list(self, user_id: str) -> list[WorkspaceProject]:
        with self._lock:
            records = self._load().get(user_id, [])
        return [WorkspaceProject.model_validate(item) for item in records]

    def create(self, user_id: str, name: str) -> WorkspaceProject:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("项目名称不能为空")
        with self._lock:
            mapping = self._load()
            records = mapping.setdefault(user_id, [])
            if any(item.get("name", "").casefold() == clean_name.casefold() for item in records):
                raise ValueError("同名项目已经存在")
            project_id = uuid.uuid4().hex
            project = WorkspaceProject(
                id=project_id,
                name=clean_name,
                workspace_id=f"{PROJECT_WORKSPACE_PREFIX}{project_id}",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            records.append(project.model_dump())
            self._save(mapping)
        return project


workspace_project_router = APIRouter(prefix="/workspace-projects", tags=["workspace-projects"])
workspace_project_store = WorkspaceProjectStore()


@workspace_project_router.get("", response_model=list[WorkspaceProject])
async def list_workspace_projects(request: Request) -> list[WorkspaceProject]:
    user_id = request.headers.get("X-User-ID", "")
    return workspace_project_store.list(user_id)


@workspace_project_router.post("", response_model=WorkspaceProject, status_code=201)
async def create_workspace_project(
    body: CreateWorkspaceProject,
    request: Request,
) -> WorkspaceProject:
    user_id = request.headers.get("X-User-ID", "")
    try:
        return workspace_project_store.create(user_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
