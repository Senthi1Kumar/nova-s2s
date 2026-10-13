# nova_hailo/cabingym/env.py
"""Gymnasium-shaped episodes over a fresh CabinEnv (no gym dependency).

reset(task) builds a new vehicle and applies the task's init state;
step(utterance) routes, gates and executes one user turn. Callers:
scripts/cabingym_eval.py, tests.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.confirm import parse_confirmation
from nova_hailo.cabingym.executor import VehicleExecutor
from nova_hailo.cabingym.nav import NavProvider
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.types import RouteOutcome

SEED_TASKS = Path(__file__).parent / "tasks" / "seed.jsonl"
RouteFn = Callable[[str, CabinEnv], "RouteOutcome | None"]


@dataclass(frozen=True)
class Turn:
    user: str
    expect_gate: tuple[str, ...] | None


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    tags: tuple[str, ...]
    init: dict[str, Any]
    turns: tuple[Turn, ...]
    expected_diff: dict[str, Any]


@dataclass
class StepInfo:
    kind: str
    verdicts: list[str]
    speak: str
    diff: list[dict[str, Any]]
    latency_ms: dict[str, float]
    result: dict[str, Any] | None


def load_tasks(path: str | Path | None = None) -> list[Task]:
    tasks: list[Task] = []
    for raw in Path(path or SEED_TASKS).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        turns = tuple(
            Turn(t["user"], tuple(t["expect_gate"]) if t.get("expect_gate") is not None else None)
            for t in d["turns"]
        )
        tasks.append(Task(
            id=d["id"], category=d.get("category", ""), tags=tuple(d.get("tags", ())),
            init={c.resolve(k): v for k, v in d.get("init", {}).items()},
            turns=turns,
            expected_diff={c.resolve(k): v for k, v in d.get("expected_diff", {}).items()},
        ))
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids in CabinGym task file")
    return tasks


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


class CabinGymEnv:
    def __init__(self, route_fn: RouteFn, *, profile: str = "ev_demo", nav: NavProvider | None = None):
        self.route_fn = route_fn
        self.profile = profile
        self._nav = nav
        self.cabin = CabinEnv(profile, nav=nav)
        self.executor = VehicleExecutor(self.cabin)

    def reset(self, task: Task | None = None) -> dict[str, Any]:
        self.cabin = CabinEnv(self.profile, nav=self._nav)
        self.executor = VehicleExecutor(self.cabin)
        if task is not None:
            self.cabin.store.apply(task.init)
        return self.observe()

    def observe(self) -> dict[str, Any]:
        return {"snapshot": self.cabin.store.snapshot(), "pending": self.executor.pending is not None}

    def step(self, utterance: str) -> tuple[dict[str, Any], StepInfo]:
        t0 = time.perf_counter()
        if self.executor.pending is not None:
            answer = parse_confirmation(utterance)
            if answer is not None:
                result = self.executor.resolve_confirm(answer)
                if result is not None:
                    return self.observe(), StepInfo(
                        "confirm", list(result["result"]["verdicts"]), result["speak"],
                        list(result["result"]["diff"]), {"route": 0.0, "exec": _ms(t0)}, result)
        self.executor.drop_pending()
        outcome = self.route_fn(utterance, self.cabin)
        route_ms = _ms(t0)
        if outcome is None:
            return self.observe(), StepInfo("none", ["none"], "", [], {"route": route_ms, "exec": 0.0}, None)
        t1 = time.perf_counter()
        result = self.executor.run(outcome)
        return self.observe(), StepInfo(
            outcome.kind, list(result["result"]["verdicts"]), result["speak"],
            list(result["result"]["diff"]), {"route": route_ms, "exec": _ms(t1)}, result)


def run_task(env: CabinGymEnv, task: Task) -> tuple[dict[str, Any], dict[str, Any], list[StepInfo]]:
    init = env.reset(task)["snapshot"]
    infos = [env.step(turn.user)[1] for turn in task.turns]
    env.cabin.adapter.settle()
    return init, env.cabin.store.snapshot(), infos


def run_oracle(task: Task, profile: str = "ev_demo") -> tuple[dict[str, Any], dict[str, Any], list[StepInfo]]:
    """Upper bound and scorer sanity check: write the expected diff directly."""
    env = CabinGymEnv(lambda text, cabin: None, profile=profile)
    init = env.reset(task)["snapshot"]
    env.cabin.store.apply(task.expected_diff)
    infos = [StepInfo("oracle", list(t.expect_gate or []), "", [], {"route": 0.0, "exec": 0.0}, None)
             for t in task.turns]
    return init, env.cabin.store.snapshot(), infos
