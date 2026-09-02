import asyncio
import textwrap

import pytest

from app.workflow.engine import WorkflowEngine, WorkflowError
from app.workflow.loader import TemplateError, instantiate
from app.workflow.store import TaskStore

TEMPLATE = textwrap.dedent(
    """
    name: demo
    display_name: 演示流程
    description: 自动 → 人工 → 自动
    variables:
      score:
        type: integer
        required: true
    steps:
      - id: collect
        name: 数据收集
        type: auto
        instruction: 收集分数 {score} 的数据
      - id: confirm
        name: 人工确认
        type: human
        assignee: alice
        instruction: 请确认结果
      - id: report
        name: 生成报告
        type: auto
        instruction: 汇总生成报告
    """
)


@pytest.fixture
def env(tmp_path):
    (tmp_path / "demo.yaml").write_text(TEMPLATE, encoding="utf-8")
    store = TaskStore(tmp_path / "tasks.json")

    async def fake_run(task, step, context):
        if step.id == "fail_step":
            raise RuntimeError("boom")
        return f"[{step.name}] done; upstream={len(context)} chars"

    engine = WorkflowEngine(str(tmp_path), fake_run)
    return tmp_path, store, engine


def run(coro):
    return asyncio.run(coro)


def make_task(env, variables=None):
    tmp_path, _, engine = env
    return run(engine.create_task(env[1], "demo", variables or {"score": 600}))


def test_create_stops_at_human_gate(env):
    task = make_task(env)
    assert task.status == "waiting_human"
    assert [s.id for s in task.history] == ["collect"]
    assert task.current.id == "confirm"
    assert task.current.status == "waiting_human"
    assert task.current.assignee == "alice"
    assert [s.id for s in task.planned] == ["report"]


def test_submit_resumes_to_finish(env):
    task = make_task(env)
    task = run(env[2].submit_human(env[1], task.id, "confirm", "画像确认无误", actor="alice"))
    assert task.status == "finished"
    assert [s.id for s in task.history] == ["collect", "confirm", "report"]
    assert task.current is None and task.planned == []
    # 人工提交被记录，历史环节不可变
    assert task.interventions[0].action == "submit"
    assert task.history[0].status == "done"


def test_skip_current_human_step(env):
    task = make_task(env)
    task = run(env[2].skip(env[1], task.id, "confirm", actor="boss"))
    assert task.status == "finished"
    skipped = [s for s in task.history if s.id == "confirm"]
    assert len(skipped) == 1 and skipped[0].status == "skipped"


def test_skip_planned_step(env):
    task = make_task(env)
    task = run(env[2].skip(env[1], task.id, "report", actor="boss"))
    assert "report" not in [s.id for s in task.planned]
    task = run(env[2].submit_human(env[1], task.id, "confirm", "ok", actor="alice"))
    assert task.status == "finished"
    assert "report" not in [s.id for s in task.history]


def test_redo_failed_current_step(env):
    tmp_path, store, _ = env
    (tmp_path / "fail.yaml").write_text(
        TEMPLATE.replace("id: collect", "id: fail_step"), encoding="utf-8"
    )
    engine = WorkflowEngine(str(tmp_path), _make_fail_runner())
    task = run(engine.create_task(store, "fail", {"score": 600}))
    assert task.status == "failed" and task.current.status == "failed"
    task = run(engine.redo(store, task.id, "fail_step", actor="boss"))
    assert task.history[0].status == "done"


def _make_fail_runner():
    calls = {"n": 0}

    async def runner(task, step, context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "recovered"

    return runner


def test_redo_completed_step_inserts_copy(env):
    task = make_task(env)
    # 当前处于人工挂起，redo 副本插入 planned 队首，不中断当前环节
    task = run(env[2].redo(env[1], task.id, "collect", actor="boss"))
    assert task.current.id == "confirm"
    assert task.planned[0].id.startswith("collect__redo")
    task = run(env[2].submit_human(env[1], task.id, "confirm", "ok", actor="alice"))
    # redo 副本在 report 之前执行
    assert [s.id for s in task.history] == ["collect", "confirm", "collect__redo1", "report"]


def test_reassign(env):
    task = make_task(env)
    task = run(env[2].reassign(env[1], task.id, "confirm", "bob", actor="boss"))
    assert task.current.assignee == "bob"
    assert task.interventions[-1].action == "reassign"


def test_missing_required_variable(env):
    with pytest.raises(TemplateError):
        instantiate(env[0], "demo", "t1", {})


def test_submit_wrong_step_rejected(env):
    task = make_task(env)
    with pytest.raises(WorkflowError):
        run(env[2].submit_human(env[1], task.id, "report", "x", actor="alice"))


def test_store_persists(env):
    task = make_task(env)
    reloaded = TaskStore(env[1].path)
    assert reloaded.get(task.id).status == task.status
