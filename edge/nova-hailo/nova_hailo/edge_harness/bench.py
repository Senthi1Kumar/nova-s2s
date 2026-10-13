"""The harness inside CabinGym episodes: direct / delegated / mixed configurations.

Callers: scripts/cabingym_eval.py, tests.

``HarnessEpisodeEnv`` routes each turn like the live gateway does: the
frontend router parses it, ``execution.decide`` picks direct or delegate, and
a delegated turn runs the backend agent synchronously against the episode's
own cabin and safety gate. ``fake_backend_factory`` is a deterministic planner
for CI; ``groq_backend_factory`` is the real one.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from nova_hailo.cabingym.confirm import parse_confirmation
from nova_hailo.cabingym.env import CabinGymEnv, RouteFn, StepInfo
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.slots import split_clauses
from nova_hailo.edge_harness.backend_agent import BackendAgent
from nova_hailo.edge_harness.execution import DELEGATE, OFFLINE, OFFLINE_SPEAK, decide
from nova_hailo.edge_harness.tasks import TaskRegistry

MULTISTEP_TASKS = Path(__file__).resolve().parents[1] / "cabingym" / "tasks" / "multistep.jsonl"

_PLANNING_WORDS = re.compile(
    r"\b(?:before (?:we|i) (?:get|arrive|leave|go)|on (?:the|my) way(?: home)?|as soon as we get going"
    r"|when (?:we|i) (?:get|arrive)|^then|it's starting to rain)\b",
    re.I,
)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


class FakePlannerLLM:
    """A deterministic stand-in for the cloud planner (CI only).

    First call: strip planning words, split the objective into clauses, parse
    each with the regex router and emit them as device tool calls. Second call:
    no tool calls, so the agent speaks the car's own result lines.
    """

    def __init__(self, cabin: Any) -> None:
        self.cabin = cabin
        self.last_tool_calls: list[dict[str, Any]] = []

    def generate(self, messages, max_tokens=None, tools=None, tool_choice=None, **kw):
        if any(m.get("role") == "assistant" for m in messages):
            self.last_tool_calls = []
            return "", None
        objective = str(messages[1]["content"]).split("\n\nCar state:")[0]
        calls: list[dict[str, Any]] = []
        for clause in split_clauses(_PLANNING_WORDS.sub(" ", objective)):
            outcome = route_vehicle_regex(clause.strip(" ,"), self.cabin)
            if outcome is None or outcome.kind != "vehicle":
                continue
            for call in outcome.calls:
                calls.append({"id": f"call_{len(calls)}", "name": f"vehicle_{call.device}",
                              "arguments": {"action": call.api, **call.args}})
        self.last_tool_calls = calls
        return "", None


def fake_backend_factory(cabin: Any, executor: Any) -> BackendAgent:
    return BackendAgent(FakePlannerLLM(cabin), cabin, executor)


def groq_backend_factory(model: str = "openai/gpt-oss-120b", budget_s: float = 20.0,
                         provider: str = "groq"):
    """A factory for the real backend on a chat-completions provider (groq | openrouter | cerebras)."""
    from nova_hailo.backends.openrouter_llm import PROVIDERS, OpenRouterLLM

    keys = PROVIDERS[provider][1]
    if not any(os.environ.get(k, "").strip() for k in keys):
        raise RuntimeError(f"{keys[0]} is not set")
    # Reasoning follows the model (openrouter_llm._default_reasoning), as on the voice path.
    llm = OpenRouterLLM(provider=provider, model=model, max_tokens=384, temperature=0.2,
                        timeout_s=budget_s)

    def factory(cabin: Any, executor: Any) -> BackendAgent:
        return BackendAgent(llm, cabin, executor, budget_s=budget_s)

    return factory


class HarnessEpisodeEnv(CabinGymEnv):
    def __init__(
        self,
        route_fn: RouteFn,
        *,
        execution: str,
        backend_factory: Any,
        online: bool = True,
        profile: str = "ev_demo",
    ) -> None:
        super().__init__(route_fn, profile=profile)
        self.execution = execution
        self.backend_factory = backend_factory
        self.online = online

    def step(self, utterance: str) -> tuple[dict[str, Any], StepInfo]:
        if self.executor.pending is not None and parse_confirmation(utterance) is not None:
            return super().step(utterance)
        t0 = time.perf_counter()
        self.executor.drop_pending()
        outcome = self.route_fn(utterance, self.cabin)
        route_ms = _ms(t0)
        online = self.online and self.backend_factory is not None
        decision = decide(utterance, outcome, mode=self.execution, online=online)
        if decision.path == OFFLINE:
            return self.observe(), StepInfo("offline", ["offline"], OFFLINE_SPEAK, [],
                                            {"route": route_ms, "exec": 0.0}, None)
        if decision.path == DELEGATE:
            t1 = time.perf_counter()
            agent = self.backend_factory(self.cabin, self.executor)
            registry = TaskRegistry(start=lambda fn: fn())
            task, _ = registry.submit("delegate", utterance, lambda h: agent.run(utterance, h))
            done = registry.get(task.id)
            steps = (done.trace or {}).get("steps", [])
            verdicts = [v for s in steps for v in s.get("verdicts", [])] or [done.status]
            diff = [d for s in steps for d in s.get("diff", [])]
            # Provider rate-limit waits are not the agent's latency.
            backend_ms = max(0.0, _ms(t1) - float((done.trace or {}).get("waited_ms") or 0.0))
            return self.observe(), StepInfo(
                "delegate", verdicts, done.speak, diff,
                {"route": route_ms, "backend": backend_ms, "exec": backend_ms},
                {"status": done.status, "trace": done.trace, "reason": done.reason,
                 "decision": decision.reason})
        if outcome is None:
            return self.observe(), StepInfo("none", ["none"], "", [],
                                            {"route": route_ms, "exec": 0.0}, None)
        t1 = time.perf_counter()
        result = self.executor.run(outcome)
        return self.observe(), StepInfo(
            outcome.kind, list(result["result"]["verdicts"]), result["speak"],
            list(result["result"]["diff"]), {"route": route_ms, "exec": _ms(t1)}, result)
