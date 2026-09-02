from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agent_runner import make_agent_runner
from app.settings import load_settings
from app.tools.base import close_http, init_http
from app.workflow import loader
from app.workflow.engine import WorkflowEngine, WorkflowError
from app.workflow.store import TaskStore

settings = load_settings()
store = TaskStore(settings.store_path)
engine = WorkflowEngine(settings.templates_dir, make_agent_runner(settings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_http(settings)
    yield
    await close_http()


app = FastAPI(title="AgentForge Workorder Service", lifespan=lifespan)


class CreateTaskRequest(BaseModel):
    template: str
    variables: dict = {}


class SubmitRequest(BaseModel):
    step_id: str
    output: str
    actor: str = "anonymous"


class SkipRequest(BaseModel):
    step_id: str
    actor: str = "anonymous"


class RedoRequest(BaseModel):
    step_id: str
    actor: str = "anonymous"


class ReassignRequest(BaseModel):
    step_id: str
    assignee: str
    actor: str = "anonymous"


@app.get("/templates")
def list_templates():
    return loader.list_templates(settings.templates_dir)


@app.post("/tasks")
async def create_task(req: CreateTaskRequest):
    try:
        task = await engine.create_task(store, req.template, req.variables)
    except (loader.TemplateError, WorkflowError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return task.model_dump(mode="json")


@app.get("/tasks")
def list_tasks(status: str | None = None, assignee: str | None = None):
    return [t.model_dump(mode="json") for t in store.list(status, assignee)]


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/submit")
async def submit_human(task_id: str, req: SubmitRequest):
    try:
        task = await engine.submit_human(store, task_id, req.step_id, req.output, req.actor)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/skip")
async def skip_step(task_id: str, req: SkipRequest):
    try:
        task = await engine.skip(store, task_id, req.step_id, req.actor)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/redo")
async def redo_step(task_id: str, req: RedoRequest):
    try:
        task = await engine.redo(store, task_id, req.step_id, req.actor)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return task.model_dump(mode="json")


@app.post("/tasks/{task_id}/reassign")
async def reassign_step(task_id: str, req: ReassignRequest):
    try:
        task = await engine.reassign(store, task_id, req.step_id, req.assignee, req.actor)
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return task.model_dump(mode="json")
