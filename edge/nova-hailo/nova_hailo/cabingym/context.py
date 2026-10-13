"""One short note of what the cabin and the trip are doing, for the agent's next turn.

Callers: nova_hailo.pipeline (rides beside the driver's question each cloud
turn, never stored in history), tests.

The note is rebuilt from the store every turn, so it is always current: the
active route and its stops, the places the last search found (numbered, so
"add the second one as a stop" resolves), a question still waiting for a yes
or no, and the last few cabin changes. It replaces what a long history used to
carry, and does not age out of the window.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from nova_hailo.cabingym import catalog as c

MAX_RESULTS = 3
MAX_STOPS = 4


def _place_names(items: Any, limit: int) -> list[str]:
    out: list[str] = []
    for item in list(items or ())[:limit]:
        name = item.get("name") if isinstance(item, Mapping) else None
        if name:
            out.append(str(name))
    return out


def _call_text(call: Any) -> str:
    args = getattr(call, "args", None) or {}
    shown = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    return f"{getattr(call, 'device', '?')}.{getattr(call, 'api', '?')}" + (f"({shown})" if shown else "")


def path_label(path: str) -> str:
    """'Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature' -> 'Row1 Driver Temperature'."""
    return " ".join(path.split(".")[-3:])


def cabin_context(
    snapshot: Mapping[str, Any],
    *,
    pending_calls: Iterable[Any] | None = None,
    recent: Iterable[tuple[str, Any]] = (),
) -> str:
    lines: list[str] = []
    if snapshot.get(c.NAV_ACTIVE):
        trip = f"Navigating to {snapshot.get(c.NAV_DEST) or 'a destination'}"
        eta = snapshot.get(c.NAV_ETA)
        if eta:
            trip += f", about {round(float(eta))} min"
        stops = _place_names(snapshot.get(c.NAV_STOPS), MAX_STOPS)
        if stops:
            trip += "; stops on the way: " + ", ".join(stops)
        lines.append(trip + ".")
    else:
        lines.append("Not navigating.")
    results = _place_names(snapshot.get(c.NAV_RESULTS), MAX_RESULTS)
    if results:
        lines.append("Last place search: " + "; ".join(f"{i}. {n}" for i, n in enumerate(results, 1)) + ".")
    pending = [_call_text(call) for call in (pending_calls or ())]
    if pending:
        lines.append("Waiting for the driver's yes or no on: " + "; ".join(pending) + ".")
    changes = [f"{path_label(p)} -> {v}" for p, v in recent]
    if changes:
        lines.append("Recent cabin changes: " + "; ".join(changes) + ".")
    return " ".join(lines)
