import json
from pathlib import Path

from app.workflow.models import TaskInstance


class TaskStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, TaskInstance] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for raw in json.load(f):
                    task = TaskInstance.model_validate(raw)
                    self._tasks[task.id] = task

    def save(self, task: TaskInstance) -> None:
        self._tasks[task.id] = task
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([t.model_dump(mode="json") for t in self._tasks.values()], f, ensure_ascii=False, indent=2)

    def get(self, task_id: str) -> TaskInstance | None:
        return self._tasks.get(task_id)

    def list(self, status: str | None = None, assignee: str | None = None) -> list[TaskInstance]:
        result = list(self._tasks.values())
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [
                t
                for t in result
                if (t.current and t.current.assignee == assignee)
                or any(s.assignee == assignee for s in t.planned)
            ]
        return sorted(result, key=lambda t: t.created_at)
