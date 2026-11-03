"""Install or refresh the paper-drafting small agents for local accounts.

The operation is idempotent by agent name: existing agents keep their IDs and
runtime configuration while their maintained prompt is refreshed.
"""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import RedisStorage
from agentscope.app.storage._model import AgentData, AgentRecord

from app.paper_writing_tools import PAPER_TEMPLATE_NAMES
from app.prompt_templates import list_prompt_templates


USERS_DIR = BASE_DIR / "data" / "users"


def _paper_templates() -> dict[str, str]:
    templates = {item["name"]: item["content"] for item in list_prompt_templates()}
    missing = [name for name in PAPER_TEMPLATE_NAMES if name not in templates]
    if missing:
        raise RuntimeError(f"缺少论文智能体模板: {', '.join(missing)}")
    return {name: templates[name] for name in PAPER_TEMPLATE_NAMES}


async def install(users: list[str], host: str, port: int, dry_run: bool) -> int:
    templates = _paper_templates()
    missing_users = [user for user in users if not (USERS_DIR / f"{user}.txt").is_file()]
    if missing_users:
        print(f"账号不存在，已跳过: {', '.join(missing_users)}")
    valid_users = [user for user in users if user not in missing_users]
    async with RedisStorage(host=host, port=port) as storage:
        for user in valid_users:
            existing = {record.data.name: record for record in await storage.list_agents(user)}
            created = updated = unchanged = 0
            for name, prompt in templates.items():
                record = existing.get(name)
                if record is None:
                    created += 1
                    if not dry_run:
                        data = AgentData(
                            name=name,
                            system_prompt=prompt,
                            context_config=ContextConfig(),
                            react_config=ReActConfig(),
                        )
                        await storage.upsert_agent(user, AgentRecord(user_id=user, data=data))
                elif record.data.system_prompt != prompt:
                    updated += 1
                    if not dry_run:
                        data = record.data.model_copy(update={"system_prompt": prompt})
                        refreshed = record.model_copy(
                            update={"data": data, "updated_at": datetime.now()},
                        )
                        await storage.upsert_agent(user, refreshed)
                else:
                    unchanged += 1
            mode = "预演" if dry_run else "完成"
            print(
                f"{user}: {mode}，新增 {created}，更新 {updated}，"
                f"未变化 {unchanged}",
            )
    return 1 if missing_users else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("users", nargs="+", help="目标账号")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(install(args.users, args.host, args.port, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
