"""How a finished deep_research result is announced, on every front door.

Callers: nova_hailo/web/realtime_session.py (LAN) and
nova_hailo/lk/nova_agent.py (LiveKit).
"""
from __future__ import annotations

from typing import Any


def research_announcement(item: dict[str, Any]) -> str:
    """The spoken line for one finished research job, or "" if there is nothing to say.

    Names the question being answered. A result can land several turns after
    it was asked, and "that research you asked for" is indistinguishable from
    a wrong answer to whatever was just said.
    """
    ready = str(item.get("announce") or "").strip()
    if ready:
        return ready  # a delegated task's result is already a spoken line
    speak = str(item.get("speak") or "").strip()
    if not speak:
        return ""
    asked = " ".join(str(item.get("user_text") or "").split())[:80].rstrip(" ,.?!")
    if asked:
        return f"About your question on {asked} — {speak}"
    return f"About that research you asked for — {speak}"


def delivery_trace_events(item: dict[str, Any], generation_id: Any) -> list[dict[str, Any]]:
    """nova.agent_trace events for a delivered delegated result, tagged with the
    generation that speaks it, so a display updates as the result is said."""
    if item.get("kind") != "delegate":
        return []
    return [
        {"type": "nova.agent_trace", "generation_id": generation_id, **trace}
        for trace in item.get("traces") or []
        if trace
    ]
