"""Warm llama-server's prefix cache with the real system+tools block."""
from __future__ import annotations
from typing import Any
import httpx


def build_prewarm_payload(system_prompt: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    chat_tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in tools]
    return {
        "model": "warm",
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": "hi"}],
        "tools": chat_tools,
        "max_tokens": 1,
        "temperature": 0.0,
    }


def prewarm_llm(base_url: str, system_prompt: str, tools: list[dict[str, Any]]) -> None:
    try:
        httpx.post(f"{base_url}/v1/chat/completions",
                   json=build_prewarm_payload(system_prompt, tools), timeout=60)
    except httpx.HTTPError:
        pass  # best-effort warm; never block startup on it
