# nova_hailo/cabingym/router_regex.py
"""Deterministic vehicle router: clause split → first matching API → grounded args.

Runs with zero connectivity; it is both the offline path and the baseline
every learned router is measured against. Callers: OemToolGateway, CabinGym
eval runner.
"""
from __future__ import annotations

import re
import time

from nova_hailo.cabingym.devices import DEVICE_ORDER
from nova_hailo.cabingym.devices.base import Api
from nova_hailo.cabingym.grounding import clarify_speak, decline_speak, ground_args
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.slots import split_clauses
from nova_hailo.cabingym.types import RouteOutcome, VehicleCall


def match_api(clause: str, cabin: CabinEnv) -> tuple[str, Api] | None:
    for name in DEVICE_ORDER:
        for api in cabin.devices[name].apis.values():
            if api.needs_live_nav and not cabin.allow_poi_search:
                continue
            if any(re.search(p, clause, re.I) for p in api.patterns):
                return name, api
    return None


def route_vehicle_regex(utterance: str, cabin: CabinEnv) -> RouteOutcome | None:
    t0 = time.perf_counter()
    calls: list[VehicleCall] = []
    clarify: str | None = None
    decline: str | None = None
    matches: list[dict[str, str]] = []
    for clause in split_clauses(utterance):
        hit = match_api(clause, cabin)
        if hit is None:
            continue
        device, api = hit
        matches.append({"clause": clause, "device": device, "api": api.name})
        g = ground_args(api, clause, cabin)
        if g.unsupported:
            decline = decline or decline_speak(api, g.unsupported)
            continue
        if g.missing:
            clarify = clarify or clarify_speak(api, g.missing, g.unresolved)
            continue
        calls.append(VehicleCall(device, api.name, g.args, g.grounding))
    trace = {"router": "regex", "matches": matches,
             "latency_ms": {"route": round((time.perf_counter() - t0) * 1000, 2)}}
    if calls:
        return RouteOutcome("vehicle", calls, trace=trace)
    if decline:
        return RouteOutcome("decline", speak=decline, trace=trace)
    if clarify:
        return RouteOutcome("clarify", speak=clarify, trace=trace)
    return None
