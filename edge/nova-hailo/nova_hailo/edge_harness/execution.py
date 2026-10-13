"""Direct or delegate: which agent handles a vehicle turn, decided without an LLM.

Callers: nova_hailo.tools.oem_tools (route_and_execute), scripts/cabingym_eval.py.

The frontend (regex router + VehicleExecutor on the Pi) keeps every turn it
fully understands: it is fast and works offline. A turn goes to the backend
agent only when it needs planning the frontend cannot do: a temporal or
conditional plan ("before we arrive"), a vague goal ("make it comfortable"),
a request the parser only partly understood, or more calls than a single
direct turn should carry. Offline, nothing is delegated: Nova does the part
it parsed, or says plainly that the plan needs a connection.

Modes reproduce the three configurations of the Qwen-Audio-Agent cockpit
benchmark: direct (frontend only), delegated (backend for every vehicle
turn) and mixed (this policy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DIRECT = "direct"
DELEGATE = "delegate"
OFFLINE = "offline"
PASS = "pass"
MODES = ("direct", "delegated", "mixed")

OFFLINE_SPEAK = (
    "I can't plan that without a connection, but I can do it one step at a time. What first?"
)
DELEGATE_ACK = "On it. I'll tell you when that's done."
BUSY_SPEAK = "I'm still working on the last one."

_TEMPORAL = re.compile(
    r"\b(?:on (?:the|my) way|before (?:we|i) (?:get|arrive|leave)|when (?:we|i) (?:get|arrive)"
    r"|after (?:we|i) (?:get|arrive)|as soon as|plan (?:a|my|the)|once (?:we|i) (?:get|arrive))\b",
    re.I,
)
_VAGUE = re.compile(
    r"\b(?:sort (?:it|that|this) out|fix (?:it|that|this)|getting dark|it'?s dark"
    r"|comfortable|cozy|cosy|get (?:the car|it|me) ready|prepare the car|freshen)\b",
    re.I,
)
_VEHICLE = re.compile(
    r"\b(?:temp(?:erature)?|warm(?:er)?|cold(?:er)?|cool(?:er)?|heat(?:er|ing|ed)?|fan|a/?c"
    r"|air ?con\w*|defrost\w*|windows?|seats?|sunroof|sunshade|shade|doors?|lock|unlock|trunk"
    r"|lights?|headlights?|hazards?|wipers?|mirrors?|volume|music|radio|navigate|navigation"
    r"|take me|drive me|route|directions?|range|fuel|charging)\b",
    re.I,
)
_QUESTION = re.compile(
    r"^\s*(?:what|what's|whats|how|who|when|where|which|why|is|are|will|does|do)\b", re.I
)


@dataclass(frozen=True)
class ExecutionDecision:
    path: str
    reason: str


def has_temporal_marker(text: str) -> bool:
    return bool(_TEMPORAL.search(text or ""))


def looks_vague(text: str) -> bool:
    return bool(_VAGUE.search(text or ""))


def looks_vehicle(text: str) -> bool:
    return bool(_VEHICLE.search(text or ""))


def _needs_backend(text: str, outcome: Any, max_direct_calls: int) -> str | None:
    """Why the backend is needed for a turn the parser recognised, or None."""
    if has_temporal_marker(text):
        return "temporal_plan"
    if looks_vague(text):
        return "vague_goal"
    if outcome is not None and len(getattr(outcome, "calls", []) or []) > max_direct_calls:
        return "too_many_calls"
    return None


def decide(
    text: str,
    outcome: Any,
    *,
    mode: str = "mixed",
    online: bool = True,
    max_direct_calls: int = 3,
) -> ExecutionDecision:
    """``outcome`` is the frontend router's RouteOutcome for ``text`` (or None)."""
    if mode not in MODES:
        raise ValueError(f"unknown execution mode: {mode!r}")
    vehicle_like = outcome is not None or (
        (looks_vehicle(text) or looks_vague(text)) and not _QUESTION.search(text or "")
    )
    if not vehicle_like:
        return ExecutionDecision(PASS, "not_vehicle")

    if mode == "direct":
        return ExecutionDecision(DIRECT, "direct_mode") if outcome else ExecutionDecision(
            PASS, "unparsed")
    if mode == "delegated":
        return ExecutionDecision(DELEGATE if online else OFFLINE, "delegated_mode")

    why = _needs_backend(text, outcome, max_direct_calls)
    if outcome is None:
        why = why or "unparsed_vehicle"
    if why is None:
        return ExecutionDecision(DIRECT, "parsed")
    if online:
        return ExecutionDecision(DELEGATE, why)
    if outcome is not None and getattr(outcome, "kind", "") == "vehicle":
        return ExecutionDecision(DIRECT, "offline_partial")
    return ExecutionDecision(OFFLINE, why)


def gateway_harness_kwargs(cfg_get: Any, registry: Any, backend: Any = None) -> dict[str, Any]:
    """OemToolGateway keywords for config ``harness.execution``; {} keeps slice-1 behaviour.

    ``cfg_get(section, key, default=None)`` is ``Config.get``.
    """
    mode = str(cfg_get("harness", "execution", default="") or "").strip().lower()
    if mode not in ("mixed", "delegated"):
        return {}
    return {"tasks": registry, "backend": backend, "execution": mode}
