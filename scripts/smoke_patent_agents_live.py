"""Live acceptance smoke test for the two patent-detail small agents."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


AGENT_NAMES = ("专利技术信息提取智能体", "专利技术方案分析智能体")
INPUTS = {
    "专利技术信息提取智能体": """请处理这一条实测专利技术数据，只依据输入生成合法JSON：
[{"pubid":"CN-SMOKE-001","tech_info":"本方法用于批量生成技术分析报告。现有逐个处理效率低、格式不统一；通过读取结构化技术文本，提取技术背景、技术领域、技术问题、技术方法和技术效果，并按统一模板输出报告。"}]""",
    "专利技术方案分析智能体": """请基于以下实测输入完成问题关系和方案分析，返回合法JSON；本次不要求额外联网检索，不能编造输入外的技术特征。
{"problem":"降低批量技术分析报告生成的人工整理成本","cur_problem_arr":[{"pubid":"CN-SMOKE-001","tech_problem":"逐个处理导致效率低且格式不统一"}],"key_problem_arr":[{"pubid":"CN-SMOKE-002","tech_problem":"需要将结构化技术信息转换为统一报告"}],"cur_solution_arr":[{"pubid":"CN-SMOKE-001","技术背景":"技术报告整理","技术领域":"报告生成","技术问题":"逐个处理效率低","技术方法":"读取结构化文本并按模板生成报告","技术效果":"提高批量处理效率"}],"key_solution_arr":[{"pubid":"CN-SMOKE-002","技术背景":"结构化信息分析","技术领域":"技术信息处理","技术问题":"技术字段分散","技术方法":"抽取背景、领域、问题、方法和效果字段","技术效果":"便于统一分析"}]}""",
}


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("AGENTFORGE_LIVE_URL", "http://127.0.0.1:30000"))
    parser.add_argument("--user", default=os.getenv("AGENTFORGE_SMOKE_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("AGENTFORGE_SMOKE_PASS", "admin123"))
    parser.add_argument("--model", default=os.getenv("AGENTFORGE_MODEL", "doubao-seed-2-1-turbo-260628"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=3)
    failures = 0
    async with httpx.AsyncClient(timeout=45.0, limits=limits) as client:
        login = await client.post(f"{base}/auth/login", json={"username": args.user, "password": args.password})
        login.raise_for_status()
        headers = {"X-User-ID": args.user}
        agents = (await client.get(f"{base}/agent/", headers=headers)).json()["agents"]
        by_name = {item["data"]["name"]: item for item in agents}
        credentials = (await client.get(f"{base}/credential/", headers=headers)).json()["credentials"]
        credential = next(item for item in credentials if item["data"]["type"] == "ark_credential")
        for name in AGENT_NAMES:
            agent_id = by_name[name]["id"]
            created = await client.post(
                f"{base}/sessions/",
                headers=headers,
                json={
                    "agent_id": agent_id,
                    "name": f"codex-live-{name}",
                    "chat_model_config": {
                        "type": "ark_credential",
                        "credential_id": credential["id"],
                        "model": args.model,
                        "parameters": {"temperature": 0.1},
                    },
                },
            )
            created.raise_for_status()
            session_id = created.json()["session_id"]
            try:
                started = await client.post(
                    f"{base}/chat/",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "input": {"name": args.user, "role": "user", "content": [{"type": "text", "text": INPUTS[name]}]},
                    },
                )
                started.raise_for_status()
                messages = {}
                for _ in range(24):
                    await asyncio.sleep(5)
                    messages = (await client.get(
                        f"{base}/sessions/{session_id}/messages",
                        headers=headers,
                        params={"agent_id": agent_id, "limit": 20},
                    )).json()
                    if not messages.get("is_running"):
                        break
                assistant = [m for m in messages.get("messages", []) if m.get("role") == "assistant"]
                text = "".join(
                    block.get("text", "")
                    for block in (assistant[-1].get("content", []) if assistant else [])
                    if isinstance(block, dict)
                )
                ok = bool(text.strip()) and "RUN_ERROR" not in text and "内部错误" not in text
                print(f"{name}: {'PASS' if ok else 'FAIL'}; running={messages.get('is_running')}; chars={len(text)}")
                if not ok:
                    failures += 1
                    print(text[:500])
            finally:
                deleted = await client.delete(
                    f"{base}/sessions/{session_id}",
                    headers=headers,
                    params={"agent_id": agent_id},
                )
                print(f"  cleanup: HTTP {deleted.status_code}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
