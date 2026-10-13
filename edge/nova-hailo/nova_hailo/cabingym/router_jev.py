# nova_hailo/cabingym/router_jev.py
"""Two-stage TypeSafe Jev (System One) router for vehicle tools.

Stage 1 asks one yes/no per device (plus the existing web/calendar/mail
questions when used from the gateway). Stage 2 asks, for at most two
chosen devices, one yes/no per API and one choice per enum slot or state
entity. Numbers and free text come from deterministic parsers. Any SDK
error, timeout or unsure answer returns None so the caller falls back to
the regex router. Callers: OemToolGateway, CabinGym eval runner.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from nova_hailo.cabingym.grounding import (
    NONE_OF_THESE, clarify_speak, decline_speak, entity_names, ground_args, place_candidates,
)
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.slots import split_clauses
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall
from nova_hailo.edge_harness.jev_router import (
    _TOOL_ORDER, _answer_map, _choice_of, _noul_of, build_questions, build_state,
    compose_jev_answers,
)
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.task_state import TaskState
from nova_hailo.edge_harness.types import Intent, RoutedIntent

logger = logging.getLogger(__name__)
PLACE_OTHER = "other"


_STATE = "`utterance`, `dialog`, `focus`, and `vehicle`"


def _noul_state(question: str, yes: str, no: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": {"question": question, "inspect": _STATE},
            "criteria": {"true": yes, "false": no}}


def _choice(question: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": {"question": question, "inspect": _STATE},
            "criteria": dict(criteria)}


def _enum_criteria(spec: Mapping[str, Any]) -> dict[str, str]:
    synonyms = spec.get("x-synonyms") or {}
    out = {}
    for value in spec["enum"]:
        words = list(synonyms.get(value, ()))
        label = value.replace("_", " ")
        out[value] = f"{label} ({', '.join(words)})" if words else label
    return out


def stage1_questions(cabin: CabinEnv, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    qs = dict(base or {})
    qs.setdefault("smalltalk", _noul_state(
        "Is `utterance` only a greeting, thanks, goodbye, or chat, with no request in `dialog` or `focus`?",
        "Greeting, thanks, goodbye or chitchat", "Asks the car or assistant to do or check something"))
    for name, dev in cabin.devices.items():
        qs[f"dev.{name}"] = _noul_state(
            f"Does `utterance` ask about the car's {dev.description}, or continue that request from `focus` or `dialog`?",
            f"Asks to control or check the {dev.description}, or continues that request",
            f"Does not involve the {dev.description}")
    return qs


def stage2_questions(devices: list[str], cabin: CabinEnv) -> dict[str, Any]:
    snap = cabin.store.snapshot()
    qs: dict[str, Any] = {}
    for d in devices:
        for api in cabin.devices[d].apis.values():
            if api.needs_live_nav and not cabin.allow_poi_search:
                continue
            key = f"{d}.{api.name}"
            qs[key] = _noul_state(
                f"Does `utterance` ask the car to {api.description}, or continue that from `focus` or `dialog`?",
                f"Asks to {api.description}, or continues it", f"Does not ask to {api.description}")
            for pname, spec in api.params.items():
                kind = str(spec.get("x-ground", ""))
                if kind in ("zone", "enum"):
                    qs[f"{key}.{pname}"] = _choice(
                        f"For '{api.description}', which {pname} does the driver mean? "
                        "Use the one named in `utterance`. If it names none and continues "
                        "`focus` or `dialog`, keep that one. Read `vehicle` before changing it.",
                        _enum_criteria(spec))
                elif kind.startswith("entity:"):
                    names = entity_names(kind[7:], snap)
                    if names:
                        qs[f"{key}.{pname}"] = _choice(
                            f"For '{api.description}', who does `utterance` mean?",
                            {**{n: n for n in names}, NONE_OF_THESE: "someone not in this list"})
                elif kind == "place":
                    names = [name for name, _ in place_candidates(snap)]
                    if names:
                        qs[f"{key}.{pname}"] = _choice(
                            f"For '{api.description}', which place does `utterance` mean?",
                            {**{n: n for n in names}, PLACE_OTHER: "a place not in this list"})
    return qs


def device_scores(answers: Mapping[str, Any], cabin: CabinEnv) -> dict[str, float]:
    return {name: _noul_of(dict(answers), f"dev.{name}") for name in cabin.devices}


def pick_devices(scores: Mapping[str, float], noul_min: float, max_devices: int) -> list[str]:
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [name for name, score in ranked if score >= noul_min][: max(1, max_devices)]


def _clause_for(keywords: str, clauses: list[str], utterance: str) -> str:
    for clause in clauses:
        if re.search(keywords, clause, re.I):
            return clause
    return utterance


def compose_stage2(
    answers: Mapping[str, Any], devices: list[str], utterance: str, cabin: CabinEnv,
    noul_min: float, stage1_scores: Mapping[str, float],
) -> RouteOutcome | None:
    answers = dict(answers)
    clauses = split_clauses(utterance) or [utterance]
    calls: list[VehicleCall] = []
    clarify: str | None = None
    decline: str | None = None
    partial: dict[str, Any] | None = None
    for d in devices:
        dev = cabin.devices[d]
        text = _clause_for(dev.keywords, clauses, utterance)
        for api in dev.apis.values():
            key = f"{d}.{api.name}"
            score = _noul_of(answers, key)
            if score < noul_min:
                continue

            def choose(pname: str, _key: str = key) -> str | None:
                return _choice_of(answers, f"{_key}.{pname}")

            g = ground_args(api, text, cabin, choose=choose)
            if g.unsupported:
                decline = decline or decline_speak(api, g.unsupported)
                continue
            if g.missing:
                clarify = clarify or clarify_speak(api, g.missing, g.unresolved)
                partial = partial or {"device": d, "api": api.name, "args": dict(g.args)}
                continue
            calls.append(VehicleCall(d, api.name, g.args, g.grounding, round(score, 3)))
    trace = {"router": "jev", "stage1": {k: round(v, 3) for k, v in stage1_scores.items()}}
    if calls:
        return RouteOutcome("vehicle", calls, trace=trace)
    if decline:
        return RouteOutcome("decline", speak=decline, trace=trace)
    if clarify:
        if partial:
            trace["partial"] = partial
        return RouteOutcome("clarify", speak=clarify, trace=trace)
    return None


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _vehicle_state(utterance: str, cabin: CabinEnv, dialog: Any = None) -> dict[str, Any]:
    if dialog is not None:
        cabin.dialog = [dict(turn) for turn in dialog if isinstance(turn, Mapping)]
    return {
        "utterance": utterance,
        "dialog": list(cabin.dialog),
        "focus": cabin.focus,
        "vehicle": cabin.observe(),
        "devices": [{"name": n, "description": d.description} for n, d in cabin.devices.items()],
    }


def route_vehicle_jev(
    utterance: str, cabin: CabinEnv, *, client: Any, timeout_sec: float = 1.0,
    noul_min: float = 0.65, max_devices: int = 2, dialog: Any = None,
) -> RouteOutcome | None:
    q = (utterance or "").strip()
    if not q:
        return None
    state = _vehicle_state(q, cabin, dialog)
    try:
        t0 = time.perf_counter()
        first = client.system_one(state, stage1_questions(cabin), timeout=timeout_sec)
        ms1 = _ms(t0)
        scores = device_scores(_answer_map(first), cabin)
        devices = pick_devices(scores, noul_min, max_devices)
        if not devices:
            return None
        t1 = time.perf_counter()
        second = client.system_one(state, stage2_questions(devices, cabin), timeout=timeout_sec)
        ms2 = _ms(t1)
    except Exception as exc:  # noqa: BLE001 -- any SDK/network failure falls back
        logger.info("cabingym_jev:fallback %s", type(exc).__name__)
        return None
    out = compose_stage2(_answer_map(second), devices, q, cabin, noul_min, scores)
    if out is not None:
        out.trace["latency_ms"] = {"stage1": ms1, "stage2": ms2}
    return out


def route_with_jev_vehicle(
    query: str, profile: CapabilityProfile, task_state: TaskState | None, cabin: CabinEnv, *,
    client: Any, timeout_sec: float, noul_min: float, max_actions: int, max_devices: int = 2,
    dialog: Any = None,
) -> list[RoutedIntent] | None:
    """Gateway entry: one stage-1 call decides vehicle vs web/calendar/mail/drive."""
    q = (query or "").strip()
    if not q:
        return None
    state = {**build_state(q, profile, task_state), **_vehicle_state(q, cabin, dialog)}
    try:
        t0 = time.perf_counter()
        first = client.system_one(state, stage1_questions(cabin, build_questions(profile)),
                                  timeout=timeout_sec)
        ms1 = _ms(t0)
    except Exception as exc:  # noqa: BLE001
        logger.info("cabingym_jev:fallback %s", type(exc).__name__)
        return None
    answers = _answer_map(first)
    scores = device_scores(answers, cabin)
    devices = pick_devices(scores, noul_min, max_devices)
    best_tool = max((_noul_of(answers, i.value) for i in _TOOL_ORDER if profile.allows(i)), default=0.0)
    if devices and scores[devices[0]] >= best_tool:
        try:
            t1 = time.perf_counter()
            second = client.system_one(state, stage2_questions(devices, cabin), timeout=timeout_sec)
            ms2 = _ms(t1)
        except Exception as exc:  # noqa: BLE001
            logger.info("cabingym_jev:stage2_fallback %s", type(exc).__name__)
            return None
        outcome = compose_stage2(_answer_map(second), devices, q, cabin, noul_min, scores)
        if outcome is not None:
            outcome.trace["latency_ms"] = {"stage1": ms1, "stage2": ms2}
            return [RoutedIntent(Intent.VEHICLE, q, {"outcome": outcome})]
        return None
    return compose_jev_answers(first, q, profile, noul_min=noul_min, max_actions=max_actions)
