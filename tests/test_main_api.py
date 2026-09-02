"""工单服务 HTTP 层（app/main.py）测试：端点语义、状态码映射、查询过滤。

隔离方式：环境变量指向 tmp_path + importlib.reload(main)，
runner 替换为假实现（不访问真实 LLM / 中台）。
"""

import importlib
import textwrap

import pytest
from starlette.testclient import TestClient

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


async def fake_runner(task, step, context):
    return f"[{step.name}] auto-ok"


@pytest.fixture
def api(tmp_path, monkeypatch):
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "demo.yaml").write_text(TEMPLATE, encoding="utf-8")

    monkeypatch.setenv("AGENTFORGE_STORE", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("AGENTFORGE_TEMPLATES_DIR", str(tpl_dir))
    monkeypatch.setenv("AGENTFORGE_FAKE_LLM", "1")
    # main.py 模块级执行 from app.agent_runner import make_agent_runner，
    # reload 时重新绑定 → patch agent_runner 模块上的原函数即可
    monkeypatch.setattr("app.agent_runner.make_agent_runner",
                        lambda settings: fake_runner)

    import app.main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:  # with 触发 lifespan
        yield client


def create_task(api, template="demo", variables=None):
    return api.post(
        "/tasks", json={"template": template,
                        "variables": variables or {"score": 600}},
    )


class TestTemplates:
    def test_lists_template(self, api):
        templates = api.get("/templates").json()
        assert any(t["name"] == "demo" for t in templates)

    def test_empty_dir_when_no_templates(self, api):
        assert isinstance(api.get("/templates").json(), list)


class TestCreateTask:
    def test_create_stops_at_human_gate(self, api):
        resp = create_task(api)
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"] == "waiting_human"
        assert task["current"]["id"] == "confirm"
        assert task["current"]["assignee"] == "alice"
        assert [s["id"] for s in task["history"]] == ["collect"]

    def test_unknown_template_400(self, api):
        resp = create_task(api, template="nope")
        assert resp.status_code == 400

    def test_missing_required_variable_400(self, api):
        resp = api.post("/tasks", json={"template": "demo", "variables": {}})
        assert resp.status_code == 400

    def test_task_persisted_and_listable(self, api):
        tid = create_task(api).json()["id"]
        assert api.get(f"/tasks/{tid}").status_code == 200
        listed = api.get("/tasks").json()
        assert any(t["id"] == tid for t in listed)


class TestGetTask:
    def test_missing_404(self, api):
        assert api.get("/tasks/nope").status_code == 404

    def test_filter_by_status(self, api):
        create_task(api)  # waiting_human
        waiting = api.get("/tasks", params={"status": "waiting_human"}).json()
        finished = api.get("/tasks", params={"status": "finished"}).json()
        assert len(waiting) == 1
        assert len(finished) == 0


class TestSubmit:
    def test_submit_resumes_to_finish(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(
            f"/tasks/{tid}/submit",
            json={"step_id": "confirm", "output": "确认无误", "actor": "alice"},
        )
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"] == "finished"
        assert [s["id"] for s in task["history"]] == [
            "collect", "confirm", "report",
        ]
        # 人工提交记录在审计
        assert task["interventions"][0]["action"] == "submit"

    def test_submit_wrong_step_409(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(
            f"/tasks/{tid}/submit",
            json={"step_id": "report", "output": "x"},
        )
        assert resp.status_code == 409

    def test_submit_missing_task_409(self, api):
        resp = api.post("/tasks/nope/submit",
                        json={"step_id": "s", "output": "x"})
        assert resp.status_code == 409


class TestSkip:
    def test_skip_current_human(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(
            f"/tasks/{tid}/skip", json={"step_id": "confirm", "actor": "boss"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "finished"

    def test_skip_planned_step(self, api):
        tid = create_task(api).json()["id"]
        api.post(f"/tasks/{tid}/skip", json={"step_id": "report"})
        api.post(f"/tasks/{tid}/submit",
                 json={"step_id": "confirm", "output": "ok"})
        task = api.get(f"/tasks/{tid}").json()
        assert task["status"] == "finished"
        assert "report" not in [s["id"] for s in task["history"]]


class TestRedo:
    def test_redo_completed_inserts_copy(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(f"/tasks/{tid}/redo",
                        json={"step_id": "collect", "actor": "boss"})
        assert resp.status_code == 200
        task = resp.json()
        # 副本插入计划队列首，当前人工环节不中断
        assert task["current"]["id"] == "confirm"
        assert task["planned"][0]["id"].startswith("collect__redo")
        # 提交后副本先于 report 执行
        api.post(f"/tasks/{tid}/submit",
                 json={"step_id": "confirm", "output": "ok"})
        task = api.get(f"/tasks/{tid}").json()
        assert [s["id"] for s in task["history"]] == [
            "collect", "confirm", "collect__redo1", "report",
        ]


class TestReassign:
    def test_reassign(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(
            f"/tasks/{tid}/reassign",
            json={"step_id": "confirm", "assignee": "bob", "actor": "boss"},
        )
        assert resp.status_code == 200
        assert resp.json()["current"]["assignee"] == "bob"

    def test_reassign_wrong_step_409(self, api):
        tid = create_task(api).json()["id"]
        resp = api.post(
            f"/tasks/{tid}/reassign",
            json={"step_id": "report", "assignee": "bob"},
        )
        assert resp.status_code == 409


class TestErrorMapping:
    """业务异常 → HTTP 状态码的完整映射。"""

    def test_400_for_template_errors(self, api):
        assert create_task(api, template="ghost").status_code == 400

    def test_404_for_missing_task(self, api):
        assert api.get("/tasks/ghost").status_code == 404

    def test_409_for_engine_errors(self, api):
        tid = create_task(api).json()["id"]
        assert api.post(
            f"/tasks/{tid}/submit", json={"step_id": "bad", "output": "x"},
        ).status_code == 409
