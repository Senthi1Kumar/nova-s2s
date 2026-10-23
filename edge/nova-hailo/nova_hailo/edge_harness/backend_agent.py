"""Backend agent: plans multi-step vehicle requests with a cloud model.

Callers: nova_hailo.tools.oem_tools (as the delegate worker, through
TaskRegistry), nova_hailo.pipeline (construction), scripts/cabingym_eval.py.

It never touches the car directly. Every tool call becomes a RouteOutcome via
``cloud_call`` and runs through the same VehicleExecutor and safety gate as a
frontend turn, so the backend can do nothing the frontend's gate would refuse.
The loop is bounded by steps and wall time; a confirm_first verdict stops it
and hands the question back to the user.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.cloud_tools import cloud_call, vehicle_tool_schemas

NETWORK_SPEAK = "I can't reach the network right now."
RATE_LIMIT_SPEAK = "The planning service is busy right now. Try again in a minute."
DONE_FALLBACK_SPEAK = "Done."

SYSTEM_PROMPT = (
    "You are the planning side of Nova, an in-car voice assistant. The user asked for "
    "something that may need several steps. Act only through the vehicle tools; each call "
    "is checked against the car's safety rules and its result tells you what really "
    "happened. Use the car state you are given; never invent state you were not told. Do "
    "not ask the user questions unless a tool result asks for confirmation. When you are "
    "done, reply with one or two short spoken sentences saying what you changed."
)

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
# Groq and OpenRouter answer a rate limit with HTTP 429 and "try again in 8.18s".
_RETRY_AFTER = re.compile(r"try again in ([0-9.]+)\s*s", re.I)


class RateLimited(RuntimeError):
    pass


def _retry_after_s(exc: Exception) -> float | None:
    """Seconds to wait for a provider rate limit, or None if this is not one."""
    text = str(exc)
    if "429" not in text:
        return None
    m = _RETRY_AFTER.search(text)
    return float(m.group(1)) if m else 5.0


def _location(cabin: Any) -> str:
    """Where the car is, for the planner: the area name and whether a real GPS fixed it."""
    store = getattr(cabin, "store", None)
    if store is None or not store.has(c.PLACE):
        return ""
    return f"\nLocation: {store.get(c.PLACE)} ({'GPS' if store.get(c.GPS_FIX) else 'simulated'})"


def _short_enough(text: str) -> bool:
    return 0 < len(text) <= 240 and len(_SENTENCE_END.findall(text)) <= 2


class BackendAgent:
    def __init__(
        self,
        llm: Any,
        cabin: Any,
        executor: Any,
        *,
        max_steps: int = 6,
        budget_s: float = 20.0,
        max_tokens: int = 384,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.llm = llm
        self.cabin = cabin
        self.executor = executor
        self.max_steps = int(max_steps)
        self.budget_s = float(budget_s)
        self.max_tokens = int(max_tokens)
        self._clock = clock
        self._sleep = sleep

    def _generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                  t0: float) -> str:
        """One model call; waits out a provider rate limit while the budget allows."""
        while True:
            try:
                text, _metrics = self.llm.generate(
                    messages, max_tokens=self.max_tokens, tools=tools, tool_choice="auto"
                )
                return text
            except Exception as exc:  # noqa: BLE001
                wait = _retry_after_s(exc)
                if wait is None:
                    raise
                if self._clock() - t0 + wait + 0.5 > self.budget_s:
                    raise RateLimited(str(exc)) from exc
                self._waited_s += wait + 0.5
                self._sleep(wait + 0.5)

    def run(self, objective: str, handle: Any) -> None:
        t0 = self._clock()
        self._waited_s = 0.0
        tools = vehicle_tool_schemas(self.cabin.devices)
        events = list(getattr(self.cabin, "recent_events", None) or [])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"{objective}\n\nCar state: {json.dumps(self.cabin.summary(), default=str)}"
                + _location(self.cabin)
                + (f"\nRecent cabin events: {' '.join(events)}" if events else "")
            )},
        ]
        steps: list[dict[str, Any]] = []
        speaks: list[str] = []
        rows: list[dict[str, Any]] = []

        def trace(speak: str) -> dict[str, Any]:
            # Same keys as a direct turn's trace (calls / gate / diff / speak), so an HMI
            # draws both the same way; steps keep the per-call detail for evaluation.
            return {"router": "backend",
                    "calls": rows,
                    "gate": [v for s in steps for v in s["verdicts"]],
                    "diff": [d for s in steps for d in s["diff"]],
                    "speak": speak,
                    "steps": steps,
                    "latency_ms": {"total": round((self._clock() - t0) * 1000, 1)},
                    "waited_ms": round(self._waited_s * 1000, 1)}

        try:
            for _ in range(self.max_steps):
                if handle.cancelled:
                    return
                if self._clock() - t0 > self.budget_s:
                    break
                text = self._generate(messages, tools, t0)
                calls = list(getattr(self.llm, "last_tool_calls", None) or [])
                if not calls:
                    # Speak what the car confirmed, never the model's own claims: a
                    # planner can say "heading home" without having set the route.
                    # Its text is used only when nothing ran (e.g. "there's no toaster").
                    text = (text or "").strip()
                    final = " ".join(speaks) or (text if _short_enough(text) else "")
                    final = final or DONE_FALLBACK_SPEAK
                    handle.complete(final, trace(final))
                    return
                messages.append({
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [
                        {"id": call["id"], "type": "function", "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        }}
                        for call in calls
                    ],
                })
                for call in calls:
                    if handle.cancelled:
                        return
                    result = self.executor.run(
                        cloud_call(call["name"], call.get("arguments") or {}, self.cabin)
                    )
                    status = str(result.get("status") or "")
                    speak = str(result.get("speak") or "").strip()
                    diff = (result.get("result") or {}).get("diff") or []
                    rows.extend((result.get("result") or {}).get("calls") or [])
                    steps.append({"tool": call["name"], "args": call.get("arguments") or {},
                                  "status": status, "diff": diff,
                                  "verdicts": list((result.get("result") or {}).get("verdicts")
                                                   or [])})
                    if status == "success" and speak:
                        speaks.append(speak)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps({"status": status, "speak": speak, "diff": diff},
                                              default=str),
                    })
                    if status == "confirm":
                        question = speak or "Should I go ahead?"
                        handle.needs_input({"kind": "confirm", "tool": call["name"]},
                                           question, trace(question))
                        return
            final = " ".join(speaks) or DONE_FALLBACK_SPEAK
            handle.complete(final, trace(final))
        except RateLimited as exc:
            handle.fail(RATE_LIMIT_SPEAK, f"rate_limited:{exc}", trace(RATE_LIMIT_SPEAK))
        except Exception as exc:  # noqa: BLE001 — a failed plan must end the task, not the session
            handle.fail(NETWORK_SPEAK, f"backend_error:{exc}", trace(NETWORK_SPEAK))


def build_backend_agent(cfg_get: Any, gateway: Any, *, llm_factory: Any = None) -> BackendAgent | None:
    """The configured backend agent for ``gateway``, or None (delegation then says offline).

    Needs ``harness.backend`` set to a chat-completions provider (groq | openrouter |
    cerebras), a gateway with a task registry and a cabin, and that provider's API key
    in the environment. ``cfg_get(section, key, default=None)`` is ``Config.get``.
    """
    import os

    from nova_hailo.backends.failover_llm import usable_provider
    from nova_hailo.backends.openrouter_llm import PROVIDERS

    provider = str(cfg_get("harness", "backend", default="") or "").strip().lower()
    if provider not in PROVIDERS:
        return None
    provider = usable_provider(provider)
    if getattr(gateway, "tasks", None) is None or getattr(gateway, "cabin", None) is None:
        return None
    key_vars = PROVIDERS[provider][1]
    if not any(os.environ.get(k, "").strip() for k in key_vars):
        return None
    if llm_factory is None:
        from nova_hailo.backends.openrouter_llm import OpenRouterLLM

        llm_factory = OpenRouterLLM
    budget_s = float(cfg_get("harness", "budget_s", default=20) or 20)
    # Reasoning follows the model (openrouter_llm._default_reasoning): gpt-oss at low
    # effort, where it cannot be off; off elsewhere.
    llm = llm_factory(
        provider=provider,
        model=str(cfg_get("harness", "backend_model", default="openai/gpt-oss-120b")
                  or "openai/gpt-oss-120b"),
        max_tokens=384,
        temperature=0.2,
        timeout_s=budget_s,
    )
    return BackendAgent(
        llm, gateway.cabin, gateway.vehicle,
        max_steps=int(cfg_get("harness", "max_steps", default=6) or 6),
        budget_s=budget_s,
    )
