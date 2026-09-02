from pathlib import Path

import yaml

from app.workflow.models import Step, StepType, TaskInstance


class TemplateError(ValueError):
    pass


def _render(text: str, variables: dict) -> str:
    try:
        return text.format(**variables)
    except KeyError as exc:
        raise TemplateError(f"模板变量缺失: {exc}") from exc


def load_template(templates_dir: str | Path, template_name: str) -> dict:
    path = Path(templates_dir) / f"{template_name}.yaml"
    if not path.exists():
        raise TemplateError(f"模板不存在: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "steps" not in data:
        raise TemplateError(f"模板缺少 steps 定义: {path}")
    return data


def instantiate(templates_dir: str | Path, template_name: str, task_id: str, variables: dict) -> TaskInstance:
    data = load_template(templates_dir, template_name)

    declared = data.get("variables") or {}
    missing = [
        name
        for name, spec in declared.items()
        if spec.get("required") and name not in variables
    ]
    if missing:
        raise TemplateError(f"缺少必填变量: {', '.join(missing)}")

    task = TaskInstance(id=task_id, template=template_name, variables=variables)
    seen_ids: set[str] = set()
    for spec in data["steps"]:
        step_id = spec["id"]
        if step_id in seen_ids:
            raise TemplateError(f"环节 id 重复: {step_id}")
        seen_ids.add(step_id)
        task.planned.append(
            Step(
                id=step_id,
                name=spec["name"],
                type=StepType(spec["type"]),
                instruction=_render(spec.get("instruction", ""), variables),
                assignee=spec.get("assignee"),
            )
        )
    return task


def list_templates(templates_dir: str | Path) -> list[dict]:
    result = []
    for path in sorted(Path(templates_dir).glob("*.yaml")):
        try:
            data = load_template(templates_dir, path.stem)
        except TemplateError:
            continue
        result.append(
            {
                "name": path.stem,
                "display_name": data.get("display_name", path.stem),
                "description": data.get("description", ""),
                "variables": data.get("variables") or {},
            }
        )
    return result
