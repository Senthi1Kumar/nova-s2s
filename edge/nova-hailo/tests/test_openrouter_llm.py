"""OpenRouter backend, tool schemas, speak budget — no Hailo, no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hmi_qt"))

import httpx

from nova_hailo.backends.openrouter_llm import (
    OpenRouterLLM,
    build_provider_block,
    resolve_or_model,
)
from nova_hailo.edge_harness.openai_tools import openai_tools_for_profile, tool_call_kwargs
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.speak_budget import speak_budget
from nova_hailo.edge_harness.task_state import TaskState
from nova_hailo.settings_store import OR_MODELS, load_settings, save_settings


def test_resolve_or_aliases():
    assert "v4-flash" in resolve_or_model("v4-flash")
    assert resolve_or_model("v3.2") == "deepseek/deepseek-v3.2"
    assert resolve_or_model("deepseek/deepseek-v4-flash-0731").endswith("0731")
    assert resolve_or_model("qwen3.8-flash") == "qwen/qwen3.8-flash"
    assert resolve_or_model("glm") == "z-ai/glm-4.7-flash"
    assert resolve_or_model("inkling") == "thinkingmachines/inkling-small"


def test_provider_block_defaults_to_latency_sort_without_pml():
    block = build_provider_block(sort="latency", preferred_max_latency_p90=None)
    assert block == {"sort": {"by": "latency"}}


def test_provider_block_opt_in_pml():
    block = build_provider_block(sort="latency", preferred_max_latency_p90=1.5)
    assert block["preferred_max_latency"] == {"p90": 1.5}


def test_provider_block_omitted_when_unset():
    assert build_provider_block(sort=None, preferred_max_latency_p90=None) is None


def test_generate_payload_has_no_preferred_max_latency_by_default():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenRouterLLM(api_key="sk-test", model="v4-flash", client=client)
    llm.generate([{"role": "user", "content": "hi"}])
    assert seen["body"]["provider"] == {"sort": {"by": "latency"}}
    assert "preferred_max_latency" not in seen["body"]["provider"]
    assert seen["body"]["session_id"]  # sticky routing stays on


def test_openai_tools_allowlist_only():
    prof = CapabilityProfile.from_list(["web_search", "check_calendar"])
    tools = openai_tools_for_profile(prof)
    names = {t["function"]["name"] for t in tools}
    assert names == {"web_search", "check_calendar"}
    assert all(t["type"] == "function" for t in tools)
    kw = tool_call_kwargs("web_search", {"query": "hailo 10h"})
    assert kw["query"] == "hailo 10h"


def test_speak_budget_scales():
    short = speak_budget(history_turns=0, backend="openrouter")
    assert short.depth == "short" and short.max_tokens == 80
    follow = speak_budget(history_turns=3, backend="openrouter")
    assert follow.depth == "followup" and follow.max_tokens == 160
    grounded = speak_budget(
        history_turns=0,
        state=TaskState(last_tool="web_search"),
        tool_name="web_search",
        backend="openrouter",
    )
    assert grounded.depth == "grounded" and grounded.voice_one_sentence is False
    hailo = speak_budget(history_turns=0, backend="cpp")
    assert hailo.max_tokens <= 48


def test_or_catalog_includes_requested_models():
    ids = {m["id"] for m in OR_MODELS}
    assert "qwen/qwen3.8-flash" in ids
    assert "z-ai/glm-4.7-flash" in ids
    assert "thinkingmachines/inkling-small" in ids


def test_settings_roundtrip(tmp_path: Path):
    p = tmp_path / "hmi_settings.json"
    save_settings({"mode": "cloud", "or_model": "deepseek/deepseek-v3.2"}, p)
    data = load_settings(p)
    assert data["mode"] == "openrouter"
    assert data["or_model"] == "deepseek/deepseek-v3.2"


def _sse_client(chunks: list[dict] | list[str]) -> httpx.Client:
    lines = []
    for c in chunks:
        payload = c if isinstance(c, str) else json.dumps(c)
        lines.append(f"data: {payload}\n")
    lines.append("data: [DONE]\n")
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"reasoning" in request.content
        assert b'"enabled": false' in request.content or b'"enabled":false' in request.content
        assert b"preferred_max_latency" not in request.content
        assert b'"by": "latency"' in request.content or b'"by":"latency"' in request.content
        assert b"session_id" in request.content
        return httpx.Response(
            200, text=body, headers={"Content-Type": "text/event-stream"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openrouter_stream_text():
    client = _sse_client(
        [
            {"choices": [{"delta": {"content": "Hello "}}]},
            {"choices": [{"delta": {"content": "there."}}]},
        ]
    )
    llm = OpenRouterLLM(api_key="sk-test", model="v4-flash", client=client)
    seen: list[str] = []
    text, metrics = llm.generate(
        [{"role": "user", "content": "hi"}],
        token_callback=seen.append,
    )
    assert text == "Hello there."
    assert "".join(seen) == text
    assert metrics.device == "openrouter"
    assert metrics.ttft_ms is not None
    assert not llm.last_tool_calls


def test_openrouter_tool_call_not_spoken():
    client = _sse_client(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "web_search",
                                        "arguments": '{"query":"tesla stock"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    )
    llm = OpenRouterLLM(api_key="sk-test", client=client)
    text, _ = llm.generate(
        [{"role": "user", "content": "tesla"}],
        tools=openai_tools_for_profile(
            CapabilityProfile.from_list(["web_search"])
        ),
    )
    assert text == ""
    assert llm.last_tool_calls[0]["name"] == "web_search"
    assert llm.last_tool_calls[0]["arguments"]["query"] == "tesla stock"
    parsed = llm.parse_tool("")
    assert parsed["name"] == "web_search"


def test_protocol_settings_extractors():
    from nova_hmi.protocol import llm_status_label, settings_payload

    assert settings_payload({"type": "nova.fsm"}) is None
    msg = {"type": "nova.settings", "mode": "openrouter"}
    assert settings_payload(msg)["mode"] == "openrouter"
    assert llm_status_label({"type": "nova.llm_status", "status": "loading"}) == "loading"
    assert (
        llm_status_label({"type": "nova.llm_status", "status": "error", "error": "no key"})
        == "no key"
    )
