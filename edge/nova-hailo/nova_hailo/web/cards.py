"""nova.card: what a display shows for an answer, a route, a list of results or a delegated result.

Callers: nova_hailo.web.realtime_session (end of a turn, and when a background
result is spoken), nova_hailo.lk.nova_agent (delivered results).

A card complements speech: the same answer, split into short lines a driver
can glance at, plus items when there is a list. Plain car actions get no card;
their nova.agent_trace already says what changed.
"""
from __future__ import annotations

import re
from typing import Any

_NAV_RESULTS = "Vehicle.Private.Nova.Nav.LastResults"
_SOURCE_TITLES = {
    "check_calendar": "Calendar",
    "check_email": "Email",
    "list_drive_files": "Drive",
}
_ANSWER_TOOLS = {"web_search", *_SOURCE_TITLES}
_MEDIA_LIST_APIS = {"play_media", "next_track", "previous_track", "resume_media"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_MAX_LINES = 4


def _lines(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text or "") if p.strip()]
    return parts[:_MAX_LINES]


def _card(kind: str, title: str, text: str, items: list[dict[str, str]] | None = None
          ) -> dict[str, Any]:
    return {"kind": kind, "title": title, "lines": _lines(text), "items": items or []}


def _title(user_text: str) -> str:
    t = " ".join((user_text or "").split())
    return t[:80] or "Answer"


def _place_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "Route")
    return str(value or "Route")


def turn_card(
    user_text: str,
    reply: str,
    tool_name: str | None,
    *,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The card for a finished turn, or None when speech alone is enough."""
    if not (reply or "").strip() or not tool_name:
        return None
    if tool_name in _ANSWER_TOOLS:
        return _card("answer", _SOURCE_TITLES.get(tool_name) or _title(user_text), reply)
    if tool_name == "delegate":
        return _card("answer", "Planning", reply)
    if tool_name != "vehicle" or not trace:
        return None
    for call in trace.get("calls") or []:
        device, api = call.get("device"), call.get("api")
        if device == "navigation" and api in ("navigate_to", "add_stop"):
            return _card("route", _place_name((call.get("args") or {}).get("destination")), reply)
        if device == "navigation" and api == "find_nearby":
            places = next((d.get("after") for d in trace.get("diff") or []
                           if d.get("path") == _NAV_RESULTS), None) or []
            items = [{"title": str(p.get("name")), "sub": str(p.get("category") or "")}
                     for p in places]
            return _card("results", "Nearby", reply, items)
        if device == "media" and api in _MEDIA_LIST_APIS:
            return _card("list", "Now playing", reply)
    return None


def delivery_card_events(item: dict[str, Any], generation_id: Any) -> list[dict[str, Any]]:
    """The nova.card for a background result as it is spoken (research or a delegated plan)."""
    speak = str(item.get("speak") or "").strip()
    if not speak:
        return []
    if item.get("kind") == "delegate":
        traces = item.get("traces") or []
        gates = [g for t in traces for g in t.get("gate") or []]
        items = [{"title": f"{c.get('device')} · {c.get('api')}", "sub": str(c.get("status") or "")}
                 for t in traces for c in t.get("calls") or []]
        title = "Needs your answer" if gates and gates[-1] == "confirm_first" else "Done"
        card = _card("answer", title, speak, items)
    elif item.get("kind") == "announce":
        card = _card("answer", "Reminder", speak)
    else:
        card = _card("answer", _title(str(item.get("user_text") or "")), speak)
    return [{"type": "nova.card", "generation_id": generation_id, **card}]
