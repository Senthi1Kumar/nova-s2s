"""exa_search_answer / web_search_answer -- SSE parsing + fail-closed path.

No network, no Hailo: MockTransport pattern from tests/test_openrouter_llm.py.
Exa's streamed answer is OpenAI-compatible SSE (choices[0].delta.content),
the same shape OpenRouterLLM.generate() already parses.
"""
from __future__ import annotations

import json

import httpx

from nova_hailo.tools.search_mcp import (
    EXA_SEARCH_URL,
    exa_search_answer,
    web_search_answer,
)


def _sse(*pieces: str, grounding: dict | None = None) -> str:
    lines = []
    for p in pieces:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}")
    if grounding is not None:
        lines.append(f"data: {json.dumps({'output': {'grounding': grounding}})}")
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def test_exa_search_answer_streams_tokens_and_returns_full_text():
    body = _sse("One ", "short ", "sentence.", grounding=[{"url": "https://example.com"}])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["stream"] is True
        assert payload["outputSchema"] == {
            "type": "text",
            "description": "One short spoken sentence answering the question.",
        }
        assert payload["systemPrompt"] == "Answer in one short spoken sentence."
        return httpx.Response(
            200, text=body, headers={"Content-Type": "text/event-stream"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    seen: list[str] = []
    result = exa_search_answer(
        "current weather in blr",
        "sk-exa-test",
        system_prompt="Answer in one short spoken sentence.",
        on_token=seen.append,
        client=client,
    )
    assert seen == ["One ", "short ", "sentence."]
    assert result["text"] == "One short sentence."
    assert result["grounding"] == [{"url": "https://example.com"}]


def test_exa_search_answer_hits_the_search_endpoint():
    seen_url = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_url["url"] = str(request.url)
        return httpx.Response(200, text=_sse("hi"), headers={"Content-Type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    exa_search_answer(
        "q", "sk-exa-test", system_prompt="sys", on_token=None, client=client
    )
    assert seen_url["url"] == EXA_SEARCH_URL


def test_exa_search_answer_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        exa_search_answer(
            "q", "sk-exa-test", system_prompt="sys", client=client
        )
    except RuntimeError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on HTTP 500")


def test_exa_search_answer_ignores_blank_and_comment_lines():
    raw = (
        ":keepalive\n\n"
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "\n"
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=raw, headers={"Content-Type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = exa_search_answer("q", "sk-exa-test", system_prompt="sys", client=client)
    assert result["text"] == "hi"


# --- web_search_answer: normalization + fail-closed wrapper -------------


def test_web_search_answer_no_api_key_fails_closed(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    out = web_search_answer("weather today", system_prompt="sys")
    assert out["ok"] is False
    assert out["status"] == "unavailable"
    assert out["reason"] == "no_exa_api_key"
    assert out["speak"]  # never empty -- pipeline always has something to say


def test_web_search_answer_empty_query_fails_closed(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "sk-exa-test")
    out = web_search_answer("   ", system_prompt="sys")
    assert out["ok"] is False
    assert out["reason"] == "empty_query"


def test_web_search_answer_exa_transport_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "sk-exa-test")

    def boom(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr("nova_hailo.tools.search_mcp.exa_search_answer", boom)
    out = web_search_answer("weather today", system_prompt="sys")
    assert out["ok"] is False
    assert out["status"] == "unavailable"
    assert "exa_answer_failed" in out["reason"]
    assert out["speak"]


def test_web_search_answer_success_streams_and_returns_speak(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "sk-exa-test")
    seen: list[str] = []

    def fake_answer(query, api_key, *, system_prompt, timeout_sec, type_="instant", on_token=None):
        assert api_key == "sk-exa-test"
        assert system_prompt == "sys"
        for piece in ["It's ", "sunny."]:
            if on_token is not None:
                on_token(piece)
        return {"text": "It's sunny.", "grounding": [{"url": "https://wx.example"}]}

    monkeypatch.setattr("nova_hailo.tools.search_mcp.exa_search_answer", fake_answer)
    out = web_search_answer(
        "weather in blr", system_prompt="sys", on_token=seen.append
    )
    assert out["ok"] is True
    assert out["speak"] == "It's sunny."
    assert out["result"]["provider"] == "exa_direct"
    assert out["result"]["grounding"] == [{"url": "https://wx.example"}]
    assert seen == ["It's ", "sunny."]


def test_web_search_answer_empty_answer_fails_closed(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "sk-exa-test")

    def fake_answer(*args, **kwargs):
        return {"text": "", "grounding": None}

    monkeypatch.setattr("nova_hailo.tools.search_mcp.exa_search_answer", fake_answer)
    out = web_search_answer("weather in blr", system_prompt="sys")
    assert out["ok"] is False
    assert out["reason"] == "exa_answer_empty"
