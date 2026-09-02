"""OpenRouter chat.completions backend (OpenAI-compatible, tools + stream)."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx

from nova_hailo.metrics import InferenceMetrics


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    total_chars += len(str(c.get("text", "")))
                else:
                    total_chars += len(str(c))
        else:
            total_chars += len(str(content))
    return max(1, total_chars // 4)

DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
# Sent as OpenRouter's native `models` array: if the primary is unavailable or
# errors, OpenRouter falls through in order without costing a second round trip.
FALLBACK_MODELS = ("thinkingmachines/inkling-small",)
OR_ALIASES = {
    # Bound to the literal id, not to DEFAULT_MODEL: these name that model, not
    # "whatever the default happens to be".
    "v4-flash": "deepseek/deepseek-v4-flash-0731",
    "flash": "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash-0731",
    # Fastest first-token of the candidates measured, but it does NOT emit tool
    # calls under this pipeline's system prompt -- selectable, never the default.
    "llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
    "llama": "meta-llama/llama-3.3-70b-instruct",
    "v32": "deepseek/deepseek-v3.2",
    "v3.2": "deepseek/deepseek-v3.2",
    "deepseek-v3.2": "deepseek/deepseek-v3.2",
    "qwen3.8-flash": "qwen/qwen3.8-flash",
    "qwen3.8": "qwen/qwen3.8-flash",
    "glm-4.7-flash": "z-ai/glm-4.7-flash",
    "glm": "z-ai/glm-4.7-flash",
    "inkling-small": "thinkingmachines/inkling-small",
    "inkling": "thinkingmachines/inkling-small",
}

# Reaching the host is a local-network + DNS operation; on a healthy link it
# is well under a second. Anything longer means the network is broken, and a
# voice turn should degrade quickly rather than leave the cabin silent.
CONNECT_TIMEOUT_S = 3.0

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Groq and Cerebras serve the same chat/completions wire format, so one client
# covers all three; only the OpenRouter-specific body fields (provider block,
# models[] fallback, session_id) have to be withheld from the others.
#   name -> (url, env key names, default model)
PROVIDERS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "openrouter": (
        OPENROUTER_URL,
        ("OPENROUTER_API_KEY", "OR_API_KEY"),
        DEFAULT_MODEL,
    ),
    # gpt-oss-120b measured 3/3 tool calls with 0/3 false fires on chitchat
    # under the production soul prompt with all six tools registered -- the
    # exact check a naive time-to-first-token bench misses.
    "groq": (
        "https://api.groq.com/openai/v1/chat/completions",
        ("GROQ_API_KEY",),
        "openai/gpt-oss-120b",
    ),
    "cerebras": (
        "https://api.cerebras.ai/v1/chat/completions",
        ("CEREBRAS_API_KEY",),
        "gpt-oss-120b",
    ),
}


def resolve_or_model(name: str | None) -> str:
    raw = (name or "").strip() or DEFAULT_MODEL
    key = raw.lower()
    return OR_ALIASES.get(key, raw)


def build_provider_block(
    sort: str | None = "latency",
    preferred_max_latency_p90: float | None = None,
) -> dict[str, Any] | None:
    """Provider routing hints for the OpenRouter payload.

    ``preferred_max_latency`` is off by default: on this deployment it
    reorders onto endpoints without warm capacity and raises TTFT rather
    than bounding it. Kept as an opt-in so the trade-off stays measurable.
    """
    block: dict[str, Any] = {}
    if sort:
        block["sort"] = {"by": sort}
    if preferred_max_latency_p90 is not None:
        block["preferred_max_latency"] = {"p90": float(preferred_max_latency_p90)}
    return block or None


class OpenRouterLLM:
    """Same generate() surface as HailoLLM; last_tool_calls after each call."""

    def __init__(
        self,
        vdevice=None,
        hef_path: str = "",
        temperature: float = 0.3,
        seed: int = 42,
        max_tokens: int = 80,
        no_think: bool = True,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 45.0,
        provider_sort: str | None = "latency",
        preferred_max_latency_p90: float | None = None,
        fallback_models: tuple[str, ...] | list[str] | None = None,
        provider: str | None = None,
        service_tier: str | None = None,
        reasoning_effort: str | None = "low",
    ):
        self.service_tier = (service_tier or "").strip().lower() or None
        # gpt-oss reasons before answering, and reasoning tokens are billed
        # against max_tokens. At an 80-token voice cap the model spent the
        # entire budget thinking and returned finish_reason=length with empty
        # content -- a silent turn. "low" leaves room for the spoken answer.
        self.reasoning_effort = (reasoning_effort or "").strip().lower() or None
        del vdevice, seed
        name = (provider or os.environ.get("NOVA_LLM_PROVIDER") or "openrouter").strip().lower()
        if name not in PROVIDERS:
            raise ValueError(f"unknown LLM provider {name!r}; expected one of {sorted(PROVIDERS)}")
        self.provider = name
        self.url, key_names, prov_default = PROVIDERS[name]
        self.api_key = (
            api_key
            or next((v for k in key_names if (v := os.environ.get(k))), "")
            or ""
        ).strip()
        raw_model = model or hef_path or ""
        if not raw_model:
            if name == "openrouter":
                raw_model = os.environ.get("OPENROUTER_MODEL") or prov_default
            else:
                # Groq/Cerebras ids are not portable. OPENROUTER_MODEL must
                # not leak onto another provider as a silent 404.
                raw_model = prov_default
        # resolve_or_model expands OpenRouter's short aliases; Groq/Cerebras ids
        # are already fully qualified and must pass through untouched.
        self.model = resolve_or_model(raw_model) if name == "openrouter" else raw_model
        self.hef_path = f"{name}:{self.model}"
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.no_think = bool(no_think)
        self.provider_block = build_provider_block(
            provider_sort, preferred_max_latency_p90
        )
        picked = FALLBACK_MODELS if fallback_models is None else tuple(fallback_models)
        # Never list the primary twice, and never fall back to itself.
        self.fallback_models = tuple(m for m in picked if m and m != self.model)
        self.last_metrics: InferenceMetrics | None = None
        self.last_tool_calls: list[dict[str, Any]] = []
        self._owns_client = client is None
        # Split the budget: a stream legitimately takes tens of seconds, but
        # *reaching* the host must not. One blanket timeout let a DNS failure
        # burn the whole 45s -- measured 40.7s of dead air before the fallback
        # line was spoken, which in a car is far worse than failing fast.
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S)
        )
        print(f"Loading LLM ({self.provider}): {self.model}")

    def generate(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        token_callback: Callable[[str], None] | None = None,
        abort_callback: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        quiet: bool = True,
        tools: list[str | dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[str, InferenceMetrics]:
        del quiet
        if not self.api_key:
            raise RuntimeError(f"{PROVIDERS[self.provider][1][0]} is not set")
        prompt_est = _estimate_prompt_tokens(messages)
        metrics = InferenceMetrics()
        metrics.no_think = self.no_think
        metrics.device = self.provider
        metrics.start(prompt_tokens_est=prompt_est)
        cap = max_tokens if max_tokens is not None else self.max_tokens
        tool_list = _normalize_tools(tools)
        sid = (
            os.environ.get("OPENROUTER_SESSION_ID")
            or getattr(self, "_session_id", None)
            or f"nova-{os.getpid()}"
        )
        self._session_id = sid
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": cap,
            "temperature": self.temperature,
        }
        # session_id, models[] and provider are OpenRouter routing extensions.
        # Groq and Cerebras reject unknown body fields with a 400, so they are
        # only sent on the provider that defines them.
        if self.provider == "groq":
            # Groq deprecated max_tokens in favour of max_completion_tokens.
            payload["max_completion_tokens"] = payload.pop("max_tokens", cap)
            if self.reasoning_effort:
                # Measured on-device against the real prompt and tools: "low"
                # is p50 1535ms vs Groq's "medium" default at 2542ms, both 5/5
                # on tool calls. reasoning_format="hidden" changed nothing, and
                # qwen3's effort="none" was slower *and* fired no tools at all.
                payload["reasoning_effort"] = self.reasoning_effort
        if self.provider == "groq" and self.service_tier:
            # Opt-in only: "auto"/"flex" are rejected with a 400 on plans that
            # do not include them, and on_demand (the default when the field is
            # absent) is what a free org gets. Prompt caching needs no flag --
            # it keys off the static prefix, and the soul prompt plus tool
            # definitions already sit ahead of the per-turn user text.
            payload["service_tier"] = self.service_tier
        if self.provider == "openrouter":
            payload["reasoning"] = {"enabled": False}
            payload["session_id"] = sid
            if self.fallback_models:
                # OpenRouter tries these in order if the primary errors or is
                # unavailable, inside the same request — no extra round trip.
                payload["models"] = [self.model, *self.fallback_models]
            if self.provider_block is not None:
                payload["provider"] = self.provider_block
        if tool_list:
            payload["tools"] = tool_list
            payload["tool_choice"] = tool_choice or "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.provider == "openrouter":
            # Attribution headers for the OpenRouter dashboard only.
            headers["HTTP-Referer"] = "https://github.com/Senthi1Kumar/nova-s2s"
            headers["X-Title"] = "Nova Hailo"
        parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        idx = 0
        try:
            with self._client.stream(
                "POST", self.url, headers=headers, json=payload
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")[:400]
                    raise RuntimeError(
                        f"{self.provider} HTTP {resp.status_code}: {body}"
                    )
                for line in resp.iter_lines():
                    if abort_callback and abort_callback():
                        break
                    if should_stop and should_stop():
                        break
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    if line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        parts.append(piece)
                        metrics.record_token(idx)
                        idx += 1
                        if token_callback:
                            token_callback(piece)
                    for tc in delta.get("tool_calls") or []:
                        i = int(tc.get("index") or 0)
                        slot = tool_acc.setdefault(
                            i,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["arguments"] += str(fn["arguments"])
        finally:
            metrics.end()

        self.last_tool_calls = []
        for slot in (tool_acc[k] for k in sorted(tool_acc)):
            name = (slot.get("name") or "").strip()
            if not name:
                continue
            raw_args = slot.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                parsed = {"query": raw_args}
            if not isinstance(parsed, dict):
                parsed = {"query": str(parsed)}
            self.last_tool_calls.append(
                {
                    "id": slot.get("id") or f"call_{name}",
                    "name": name,
                    "arguments": parsed,
                }
            )
        text = "".join(parts).strip()
        self.last_metrics = metrics
        return text, metrics

    def parse_tool(self, raw: str):
        del raw
        if not self.last_tool_calls:
            return None
        first = self.last_tool_calls[0]
        return {"name": first["name"], "arguments": first.get("arguments") or {}}

    def clear(self):
        self.last_tool_calls = []

    def release(self):
        self.clear()
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass


def _normalize_tools(
    tools: list[str | dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except json.JSONDecodeError:
                continue
        if isinstance(t, dict):
            out.append(t)
    return out
