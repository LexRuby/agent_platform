"""agent-service（形态 B）E2E 冒烟：建 agent → 建会话（豆包）→ 对话 → 收流式回复。

用法: python scripts/smoke_agent_service.py [base_url]
默认 base_url = http://localhost:8300，用户 ID = smoke（测试数据，跑完可留可删）
"""

import asyncio
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8300"
USER = "smoke"
ARK_KEY = os.environ.get("ARK_API_KEY", "")
ARK_MODEL = os.environ.get(
    "AGENTFORGE_MODEL", "doubao-seed-2-1-turbo-260628",
)


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as c:
        headers = {"X-User-ID": USER, "Content-Type": "application/json"}

        # 1. 凭据（豆包 = OpenAI 兼容）
        cred = await c.post(
            f"{BASE}/credential/",
            headers=headers,
            json={
                "data": {
                    "type": "openai_credential",
                    "name": "smoke-doubao",
                    "api_key": ARK_KEY,
                    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                },
            },
        )
        cred.raise_for_status()
        cred_id = cred.json()["credential_id"]
        print(f"[1] 凭据: {cred_id}")

        # 2. Agent
        agent = await c.post(
            f"{BASE}/agent/",
            headers=headers,
            json={
                "name": "冒烟助手",
                "system_prompt": "你是高考志愿咨询助手，回答务必简洁。",
                "invite_config": {"description": "smoke test agent"},
            },
        )
        agent.raise_for_status()
        agent_id = agent.json()["agent_id"]
        print(f"[2] Agent: {agent_id}")

        # 3. 会话（挂豆包模型）
        session = await c.post(
            f"{BASE}/sessions/",
            headers=headers,
            json={
                "agent_id": agent_id,
                "name": "smoke-session",
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": cred_id,
                    "model": ARK_MODEL,
                    "parameters": {"temperature": 0.3},
                },
            },
        )
        session.raise_for_status()
        session_id = session.json()["session_id"]
        print(f"[3] 会话: {session_id}")

        # 4~5. 订阅事件流（SSE，先订阅后触发），收集助手回复
        got_text = []
        seen: set = set()
        async with c.stream(
            "GET",
            f"{BASE}/sessions/{session_id}/stream",
            params={"agent_id": agent_id},
            headers={"X-User-ID": USER},
        ) as resp:
            resp.raise_for_status()
            # 订阅就绪后再触发对话
            trig = await c.post(
                f"{BASE}/chat/",
                headers=headers,
                json={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "input": {
                        "name": USER,
                        "role": "user",
                        "content": [{"type": "text", "text": "用一句话介绍你自己"}],
                    },
                },
            )
            trig.raise_for_status()
            print("[4] 对话已触发")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type", "?")
                if etype not in seen:
                    seen.add(etype)
                    print(f"    [evt] {etype}")
                # 文本增量（delta 可能是纯文本，也可能是块对象）
                if etype == "TEXT_BLOCK_DELTA":
                    delta = evt.get("delta", "")
                    if isinstance(delta, str):
                        got_text.append(delta)
                    elif delta.get("type") == "text":
                        got_text.append(delta.get("text", ""))
                # 本轮流结束（连接保持打开，后续轮次复用）
                if etype in ("REPLY_END", "RUN_ERROR"):
                    break

        reply = "".join(got_text)
        print(f"[5] 助手回复: {reply[:120]}")
        assert reply.strip(), "未收到任何回复文本"

        # 清理测试数据
        await c.delete(f"{BASE}/sessions/{session_id}", headers=headers)
        await c.delete(f"{BASE}/agent/{agent_id}", headers=headers)
        await c.delete(f"{BASE}/credential/{cred_id}", headers=headers)
        print("[6] 测试数据已清理")
        print("PASS")


if __name__ == "__main__":
    asyncio.run(main())
