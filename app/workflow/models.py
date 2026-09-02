from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class StepType(str, Enum):
    auto = "auto"
    human = "human"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting_human = "waiting_human"
    done = "done"
    skipped = "skipped"
    failed = "failed"


class TaskStatus(str, Enum):
    running = "running"
    waiting_human = "waiting_human"
    finished = "finished"
    failed = "failed"


class Step(BaseModel):
    id: str
    name: str
    type: StepType
    instruction: str = ""
    assignee: str | None = None
    status: StepStatus = StepStatus.pending
    output: str | None = None
    error: str | None = None


class Intervention(BaseModel):
    at: str
    actor: str
    action: str
    step_id: str | None = None
    detail: dict = Field(default_factory=dict)


class TaskInstance(BaseModel):
    id: str
    template: str
    variables: dict = Field(default_factory=dict)
    # 已执行环节：不可变事实（含 done / skipped），人工干预与审计的依据
    history: list[Step] = Field(default_factory=list)
    # 当前环节：仅允许取消重做
    current: Step | None = None
    # 计划环节：可自由增删换序（W1 暂不开放编辑，W2 表单式）
    planned: list[Step] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.running
    interventions: list[Intervention] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def find_step(self, step_id: str) -> Step | None:
        if self.current and self.current.id == step_id:
            return self.current
        for step in self.planned:
            if step.id == step_id:
                return step
        for step in self.history:
            if step.id == step_id:
                return step
        return None

    def log(self, action: str, actor: str, step_id: str | None = None, **detail) -> None:
        self.interventions.append(
            Intervention(
                at=datetime.now().isoformat(timespec="seconds"),
                actor=actor,
                action=action,
                step_id=step_id,
                detail=detail,
            )
        )
        self.touch()
