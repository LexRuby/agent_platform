import uuid
from typing import Awaitable, Callable

from app.workflow import loader
from app.workflow.models import Step, StepStatus, TaskInstance, TaskStatus

RunAutoStep = Callable[[TaskInstance, Step, str], Awaitable[str]]


class WorkflowError(ValueError):
    pass


class WorkflowEngine:
    """工单引擎：自动环节委托给智能体执行，人工环节挂起等待提交。

    干预只作用于 current（取消重做）与 planned（增删换序，W2 开放）；
    history 保持不可变，作为事实与审计依据。
    """

    def __init__(self, templates_dir: str, run_auto_step: RunAutoStep):
        self.templates_dir = templates_dir
        self.run_auto_step = run_auto_step

    async def create_task(self, store, template: str, variables: dict) -> TaskInstance:
        task_id = uuid.uuid4().hex[:12]
        task = loader.instantiate(self.templates_dir, template, task_id, variables)
        store.save(task)
        await self._advance(store, task)
        store.save(task)
        return task

    async def submit_human(self, store, task_id: str, step_id: str, output: str, actor: str) -> TaskInstance:
        task = self._require(store, task_id)
        if not task.current or task.current.id != step_id:
            raise WorkflowError(f"环节 {step_id} 不是当前环节")
        if task.current.type.value != "human":
            raise WorkflowError("仅人工环节支持提交")
        if task.status != TaskStatus.waiting_human:
            raise WorkflowError(f"任务状态 {task.status} 不可提交")
        task.current.output = output
        task.current.status = StepStatus.done
        task.log("submit", actor, step_id)
        self._close_current(task)
        await self._advance(store, task)
        store.save(task)
        return task

    async def skip(self, store, task_id: str, step_id: str, actor: str) -> TaskInstance:
        task = self._require(store, task_id)
        if task.current and task.current.id == step_id:
            if task.current.status == StepStatus.running:
                raise WorkflowError("环节执行中，不可跳过")
            task.current.status = StepStatus.skipped
        else:
            step = next((s for s in task.planned if s.id == step_id), None)
            if step is None:
                raise WorkflowError(f"环节不存在或不在计划中: {step_id}")
            task.planned.remove(step)
            step.status = StepStatus.skipped
        task.log("skip", actor, step_id)
        if task.current and task.current.status == StepStatus.skipped:
            self._close_current(task)
            await self._advance(store, task)
        store.save(task)
        return task

    async def redo(self, store, task_id: str, step_id: str, actor: str) -> TaskInstance:
        """重做：失败的当前环节直接重跑；已完成环节则插入一个全新副本重新执行。"""
        task = self._require(store, task_id)
        step = task.find_step(step_id)
        if step is None:
            raise WorkflowError(f"环节不存在: {step_id}")
        if step is task.current:
            if step.status != StepStatus.failed:
                raise WorkflowError("仅失败或未开始的当前环节可直接重做")
            step.status = StepStatus.pending
            step.error = None
            task.log("redo", actor, step_id)
            await self._advance(store, task)
        else:
            fresh = Step(
                id=f"{step_id}__redo{self._redo_seq(task, step_id)}",
                name=step.name,
                type=step.type,
                instruction=step.instruction,
                assignee=step.assignee,
            )
            if task.current is None:
                task.current = fresh
                await self._advance(store, task)
            else:
                task.planned.insert(0, fresh)
            task.log("redo", actor, step_id)
        store.save(task)
        return task

    async def reassign(self, store, task_id: str, step_id: str, assignee: str, actor: str) -> TaskInstance:
        task = self._require(store, task_id)
        step = task.find_step(step_id)
        if step is None or step.type.value != "human":
            raise WorkflowError(f"人工环节不存在: {step_id}")
        old = step.assignee
        step.assignee = assignee
        task.log("reassign", actor, step_id, **{"from": old, "to": assignee})
        store.save(task)
        return task

    async def _advance(self, store, task: TaskInstance) -> None:
        while True:
            if task.current is None:
                if not task.planned:
                    task.status = TaskStatus.finished
                    task.touch()
                    return
                task.current = task.planned.pop(0)
            step = task.current
            if step.type.value == "human":
                step.status = StepStatus.waiting_human
                task.status = TaskStatus.waiting_human
                task.touch()
                return
            step.status = StepStatus.running
            task.touch()
            store.save(task)
            try:
                context = self._context(task, step)
                step.output = await self.run_auto_step(task, step, context)
                step.status = StepStatus.done
                self._close_current(task)
            except Exception as exc:
                step.status = StepStatus.failed
                step.error = str(exc)
                task.status = TaskStatus.failed
                task.touch()
                store.save(task)
                return

    def _context(self, task: TaskInstance, step: Step) -> str:
        """把前面环节的产出注入当前环节，保持链式上下文。"""
        parts = []
        for s in task.history:
            if s.output:
                parts.append(f"[{s.name}]\n{s.output}")
        return "\n\n".join(parts)

    def _close_current(self, task: TaskInstance) -> None:
        if task.current is not None:
            task.history.append(task.current)
            task.current = None
            task.status = TaskStatus.running
            task.touch()

    @staticmethod
    def _redo_seq(task: TaskInstance, step_id: str) -> int:
        n = sum(1 for s in task.history if s.id.startswith(f"{step_id}__redo"))
        n += sum(1 for s in task.planned if s.id.startswith(f"{step_id}__redo"))
        return n + 1

    @staticmethod
    def _require(store, task_id: str) -> TaskInstance:
        task = store.get(task_id)
        if task is None:
            raise WorkflowError(f"任务不存在: {task_id}")
        return task
