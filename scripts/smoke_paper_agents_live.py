"""Live acceptance test for the seven paper-writing agents.

This script intentionally uses the deployed HTTP service and is not part of
the pytest suite. It creates one short-lived session per paper agent, waits
for the real model response, prints a compact result, and removes the session.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv


PAPER_NAMES = (
    "论文研究现状智能体",
    "论文科学问题智能体",
    "论文方法论智能体",
    "论文实验与结果智能体",
    "论文讨论智能体",
    "论文结论智能体",
    "论文关键词智能体",
)
PROMPTS = {
    "论文研究现状智能体": "请用两三段说明联邦学习隐私保护与通信效率的研究现状，必须区分正式文献证据、网页线索和待核验项，并包含公开可审计摘要。",
    "论文科学问题智能体": "请围绕联邦学习隐私保护与通信效率，给出痛点、科学挑战、研究缺口和待核验项；没有证据的内容标为假设。",
    "论文方法论智能体": "请为联邦学习隐私保护与通信效率设计一个可复现的方法方案，列出输入输出、关键模块、验证指标和缺失信息。",
    "论文实验与结果智能体": "没有真实数据。请只输出联邦学习通信效率实验设计、待填结果表、复现清单和公开可审计摘要，不得编造结果。",
    "论文讨论智能体": "没有真实实验结果。请输出联邦学习通信效率论文的讨论框架、局限性和待验证机制假设，不得把假设写成结果。",
    "论文结论智能体": "没有真实 Results 和 Discussion。请输出结论框架、证据缺口、贡献点草案和未来工作，不得声称方法有效。",
    "论文关键词智能体": "请从‘联邦学习隐私保护通信效率’这个主题生成分层中文和英文关键词，说明术语核验和去重依据。",
}


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("AGENTFORGE_LIVE_URL", "http://127.0.0.1:30000"))
    parser.add_argument("--user", default=os.getenv("AGENTFORGE_SMOKE_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("AGENTFORGE_SMOKE_PASS", "admin123"))
    parser.add_argument("--model", default=os.getenv("AGENTFORGE_MODEL", "doubao-seed-2-1-turbo-260628"))
    parser.add_argument("--only", choices=PAPER_NAMES)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)
    async with httpx.AsyncClient(timeout=45.0, limits=limits) as client:
        login = await client.post(f"{base}/auth/login", json={"username": args.user, "password": args.password})
        login.raise_for_status()
        headers = {"X-User-ID": args.user}
        agents = (await client.get(f"{base}/agent/", headers=headers)).json()["agents"]
        by_name = {item["data"]["name"]: item for item in agents}
        credentials = (await client.get(f"{base}/credential/", headers=headers)).json()["credentials"]
        credential = next(item for item in credentials if item["data"]["type"] == "ark_credential")
        failures = 0
        names = (args.only,) if args.only else PAPER_NAMES
        for name in names:
            record = by_name[name]
            agent_id = record["id"]
            session = await client.post(
                f"{base}/sessions/",
                headers=headers,
                json={
                    "agent_id": agent_id,
                    "name": f"codex-live-{name}",
                    "chat_model_config": {
                        "type": "ark_credential",
                        "credential_id": credential["id"],
                        "model": args.model,
                        "parameters": {"temperature": 0.2},
                    },
                },
            )
            session.raise_for_status()
            session_id = session.json()["session_id"]
            try:
                reply = await client.post(
                    f"{base}/chat/",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "input": {"name": args.user, "role": "user", "content": [{"type": "text", "text": PROMPTS[name]}]},
                    },
                )
                reply.raise_for_status()
                messages: dict[str, Any] = {}
                for _ in range(36):
                    await asyncio.sleep(5)
                    messages = (await client.get(f"{base}/sessions/{session_id}/messages", headers=headers, params={"agent_id": agent_id, "limit": 20})).json()
                    if not messages.get("is_running"):
                        break
                assistant = [m for m in messages.get("messages", []) if m.get("role") == "assistant"]
                text = "".join(block.get("text", "") for block in assistant[-1].get("content", []) if isinstance(block, dict)) if assistant else ""
                ok = bool(text.strip()) and not any(token in text for token in ("RUN_ERROR", "调用失败", "内部错误"))
                print(f"{name}: {'PASS' if ok else 'FAIL'}; running={messages.get('is_running')}; chars={len(text)}")
                if not ok:
                    failures += 1
                    print(text[:500])
            finally:
                deleted = await client.delete(f"{base}/sessions/{session_id}", headers=headers, params={"agent_id": agent_id})
                print(f"  cleanup: HTTP {deleted.status_code}")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
