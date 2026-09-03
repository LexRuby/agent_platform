"""LLM 直连工具：供推荐成员、任务归档总结等叠加层功能调用。

独立于官方 credential/模型体系（那些需要会话上下文），这里直接用
httpx 调 ARK 的 chat completions。API key 从环境变量 ``ARK_API_KEY``
读取（与 ark_credential.py 心跳一致）。

测试时 monkeypatch :func:`llm_chat` 即可，不触网。
"""

import json
import logging
import os

import httpx

_logger = logging.getLogger("agentforge.llm_utils")

ARK_BASE_URL = os.environ.get(
    "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3",
)
DEFAULT_MODEL = os.environ.get("AGENTFORGE_LLM_MODEL", "doubao-seed-2-1-turbo-260628")
TIMEOUT = 60.0


async def llm_chat(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """调 LLM 返回纯文本回复。

    Args:
        prompt: 用户指令。
        system: 可选 system 提示。
        model: 模型名，默认读环境变量。
        api_key: 显式 key，默认读 ``ARK_API_KEY``。

    Raises:
        RuntimeError: 未配置 key 或调用失败。
    """
    key = api_key or os.environ.get("ARK_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 ARK_API_KEY，无法调用大模型")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{ARK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model or DEFAULT_MODEL,
                "messages": messages,
                "temperature": 0.2,
            },
        )
        r.raise_for_status()
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应格式异常: {e}") from e


async def llm_chat_json(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict | list:
    """调 LLM 并解析 JSON 输出（容忍 ```json 围栏与前后杂文）。"""
    text = await llm_chat(prompt, system=system, model=model, api_key=api_key)
    return _extract_json(text)


def _extract_json(text: str):
    """从 LLM 回复中抽取 JSON（围栏代码块或首个平衡对象/数组）。"""
    text = text.strip()
    # ```json ... ``` 围栏
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith(("{", "[")):
                text = part
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 首个平衡 {}/[] 片段：取文本中更早出现的起始括号，
    # 否则 [{"x":1}] 会错误返回内层 {"x":1}（丢失数组语义与后续元素）
    start_candidates = [ch for ch in ("{", "[") if text.find(ch) != -1]
    if not start_candidates:
        raise ValueError(f"无法从 LLM 回复中解析 JSON: {text[:200]!r}")
    start_ch = min(start_candidates, key=lambda ch: text.find(ch))
    end_ch = "}" if start_ch == "{" else "]"
    depth = 0
    for i in range(text.find(start_ch), len(text)):
        if text[i] == start_ch:
            depth += 1
        elif text[i] == end_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[text.find(start_ch) : i + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError(f"无法从 LLM 回复中解析 JSON: {text[:200]!r}")
