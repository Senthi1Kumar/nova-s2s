"""Per-turn tool routing for the live request path: wraps ``ToolRouter``
(Task 4) with a memoization cache keyed on the registry identity, so the
router index is built once per process (registry is a process singleton),
not rebuilt on every turn.

``NOVA_TOOL_ROUTE_MODE``:
  - ``force`` (default): lexical top-k + named tool_choice / short-circuit path
  - ``full`` / ``off``: every registered tool, ``tool_choice=auto`` (model decides);
    still blocks confirm/stop/low-signal from inventing calls
  - ``model``: speak LLM gets ``tool_choice=none``; tool pick/execute is
    ``POST /tools/agent`` (LFM2.5-230M)
"""
from __future__ import annotations

import os
import re
from typing import Any

from nova.tools.base import NovaTool
from nova.tools.router import ToolRouter, _expand_synonyms, _tokenize

_router_cache: dict[int, ToolRouter] = {}


def route_mode() -> str:
    """``force`` | ``full`` | ``model``. ``off`` is an alias for ``full``."""
    raw = (os.environ.get("NOVA_TOOL_ROUTE_MODE") or "force").strip().lower()
    if raw in {"full", "off", "auto", "all"}:
        return "full"
    if raw in {"model", "agent", "router"}:
        return "model"
    return "force"


def _all_tools(registry: dict[str, NovaTool]) -> list[dict[str, Any]]:
    return [tool.to_function_tool() for tool in registry.values()]


def _get_router(registry: dict[str, NovaTool]) -> ToolRouter:
    key = id(registry)
    router = _router_cache.get(key)
    if router is None:
        router = ToolRouter(registry)
        _router_cache[key] = router
    return router


def route_tools(
    registry: dict[str, NovaTool], query: str, k: int = 8, pinned: set[str] | None = None
) -> list[dict[str, Any]]:
    return _get_router(registry).top_k(query, k=k, pinned=pinned)


# Gemma-4-E2B via llama.cpp often narrates ("Checking your emails") under
# tool_choice=auto, and still sometimes under "required". When the lexical
# router has a clear hit, force the top tool by name (live probe: forced
# name → reliable tool_calls). Keep "required" only for set_hvac so the
# model can emit multiple same-name calls for multi-zone climate.
_REQUIRED_SCORE = 2.0
_FORCE_MARGIN = 1.0  # top must beat #2 by this much before we force-by-name
_MULTI_CALL_TOOLS = frozenset({"set_hvac"})

# Bare "check" / "confirm" / "yes" must not force a tool — live logs showed
# "Check for Amazon" → check_email and "Confirm." → send_email.
_EMAIL_ANCHORS = frozenset({"email", "emails", "mail", "mails", "inbox", "gmail"})
_CALENDAR_ANCHORS = frozenset({
    "calendar", "schedule", "appointment", "appointments", "event", "events",
    "meeting", "meetings",
})
_CALENDAR_DAY_WORDS = frozenset({
    "today", "tomorrow", "tonight", "week",
})
_MEETING_NOUNS = frozenset({
    "meeting", "meetings", "appointment", "appointments", "event", "events",
})
_CREATE_CAL_VERBS = frozenset({"create", "add", "book", "make"})
_WEATHER_ANCHORS = frozenset({
    "weather", "forecast", "outside", "outdoor", "outdoors",
    "rain", "raining", "humid", "humidity", "temp", "temperature",
})
_NEWS_WORDS = frozenset({"news", "headline", "headlines", "breaking"})
_WINDOW_WORDS = frozenset({"window", "windows"})
_OPENING_ACT_WORDS = frozenset({"open", "close", "shut", "roll"})
_STATUS_WORDS = frozenset({"check", "status", "state", "are", "is", "position"})
_CONFIRM_ONLY = frozenset({"confirm", "confirmed", "yes", "yeah", "yep", "ok", "okay", "sure"})
_STOP_ONLY = frozenset({"stop", "stopped", "enough", "quiet", "silence"})
_DELETE_WORDS = frozenset({"delete", "cancel", "remove", "clear"})
_REMINDER_WORDS = frozenset({"reminder", "reminders", "remind"})
_FOLDER_WORDS = frozenset({"folder", "directory", "dir"})
_DRIVE_WORDS = frozenset({"drive", "docs", "document", "documents", "folder", "gdrive"})
_CREATE_DRIVE_VERBS = frozenset({"create", "add", "make", "new"})
_RESEARCH_WORDS = frozenset({"research", "investigate"})
_LOW_SIGNAL_OK = frozenset({
    "hi", "hey", "hello", "yo", "sup", "thanks", "thank", "ok", "okay", "sure",
    "yeah", "yep", "yup", "nah", "no", "yes", "bye", "goodbye", "there", "he",
    "hmm", "huh", "ah", "oh", "um", "uh",
})


def _query_tokens(query: str) -> set[str]:
    return _expand_synonyms(_tokenize(query))


def _is_confirm_only(query: str) -> bool:
    raw = set(re.findall(r"[a-z0-9]+", query.lower()))
    return bool(raw) and raw <= (_CONFIRM_ONLY | {"please"})


def _is_stop_only(query: str) -> bool:
    """Bare stop / stop there — do not invent a tool call."""
    raw = set(re.findall(r"[a-z0-9]+", query.lower()))
    return bool(raw) and raw <= (_STOP_ONLY | {"please", "there", "now", "that"})


def _is_low_signal(query: str) -> bool:
    """Greeting / filler / STT crumbs — never invent a tool call under auto."""
    raw = set(re.findall(r"[a-z0-9]+", query.lower()))
    if not raw:
        return True
    if len(raw) <= 2 and raw <= _LOW_SIGNAL_OK:
        return True
    # Collapsed STT like "YeahYeah" / single nonsense token.
    if len(raw) == 1:
        only = next(iter(raw))
        if only in _LOW_SIGNAL_OK or len(only) <= 3:
            return True
        if only.startswith("yeah") or only.startswith("hey"):
            return True
    return False


def _nonempty_tools(
    tools: list[dict[str, Any]], registry: dict[str, NovaTool], k: int = 3
) -> list[dict[str, Any]]:
    """UI rejects tools:[] (session deep-merge sticks empty). Always keep ≥1 schema."""
    if tools:
        return tools[: max(k, 1)]
    names = list(registry.keys())[:k]
    return [registry[n].to_function_tool() for n in names if n in registry]


def _wants_research(query: str, q_tokens: set[str]) -> bool:
    if q_tokens & _RESEARCH_WORDS:
        return True
    q = query.lower()
    return bool(re.search(r"\b(?:look\s+(?:into|up)|dig\s+into|deep\s+dive)\b", q))


def _normalize_stt_query(query: str) -> str:
    """Repair common SenseVoice garble so lexical forces still fire."""
    q = query or ""
    # calendara / calendarra / candler → calendar
    q = re.sub(r"\bcalend\w*\b", "calendar", q, flags=re.I)
    q = re.sub(r"\bcandler\w*\b", "calendar", q, flags=re.I)
    # tomorrowrow / tomorror → tomorrow
    q = re.sub(r"\btomorrow\w*\b", "tomorrow", q, flags=re.I)
    # "check my E" / "checked my E" / "check my e." (email truncated by STT)
    q = re.sub(
        r"\bcheck(?:ed)?\s+my\s+e\b\.?",
        "check my email",
        q,
        flags=re.I,
    )
    return q


def _has_explicit_date(query: str) -> bool:
    q = query.lower()
    if re.search(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", q):
        return True
    if re.search(
        r"\b(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?\b",
        q,
    ):
        return True
    if re.search(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?"
        r"(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\b",
        q,
    ):
        return True
    return False


def route_turn(
    registry: dict[str, NovaTool], query: str, k: int = 8, pinned: set[str] | None = None
) -> dict[str, Any]:
    """Return routed tool schemas plus a recommended tool_choice for this turn."""
    pinned = pinned or set()
    query = _normalize_stt_query(query)
    mode = route_mode()
    router = _get_router(registry)
    tools = (
        _all_tools(registry)
        if mode == "full"
        else router.top_k(query, k=k, pinned=pinned)
    )
    top_name = tools[0]["name"] if tools else ""
    top_score = router.score(query, top_name) if top_name and mode != "full" else 0.0
    second_score = 0.0
    if mode != "full" and len(tools) > 1:
        second_score = router.score(query, tools[1]["name"])

    q_tokens = _query_tokens(query)
    tool_choice: Any = "auto"

    # Confirmation-only turns: never invent a forced tool; honor pins if any.
    # Without pins, tool_choice=none — live log showed "Confirm." under auto
    # still firing send_email from chat residue after a failed payment force.
    if _is_confirm_only(query):
        if pinned:
            tools = [t for t in tools if t["name"] in pinned] or [
                registry[n].to_function_tool() for n in pinned if n in registry
            ]
            tools = [t for t in tools if t]
            if len(pinned) == 1:
                only = next(iter(pinned))
                tool_choice = {"type": "function", "name": only}
            else:
                tool_choice = "required"
        else:
            tools = _nonempty_tools(tools, registry)
            tool_choice = "none"
        return {
            "tools": tools,
            "tool_choice": tool_choice,
            "top_score": top_score,
            "top_name": (next(iter(pinned)) if pinned else None),
        }

    if _is_stop_only(query) or _is_low_signal(query):
        # Low-signal turns still honor non-confirm pins (e.g. sticky web_search).
        if pinned and not _is_stop_only(query):
            pinned_tools = [registry[n].to_function_tool() for n in pinned if n in registry]
            if pinned_tools:
                return {
                    "tools": pinned_tools,
                    "tool_choice": "required",
                    "top_score": top_score,
                    "top_name": pinned_tools[0]["name"],
                }
        return {
            "tools": _nonempty_tools(tools if mode != "full" else _all_tools(registry), registry),
            "tool_choice": "none",
            "top_score": top_score,
            "top_name": None,
        }

    # Full toolbox: model picks tool + args. No named force → no short-circuit.
    if mode == "full":
        if pinned:
            # Keep pending confirm tools visible alongside the full set.
            for name in pinned:
                if name in registry and all(t["name"] != name for t in tools):
                    tools.append(registry[name].to_function_tool())
        return {
            "tools": tools,
            "tool_choice": "auto",
            "top_score": top_score,
            "top_name": None,
        }

    # Model/agent mode: speak LLM never gets the toolbox; /tools/agent runs 230M.
    if mode == "model":
        return {
            "tools": _nonempty_tools([], registry),
            "tool_choice": "none",
            "top_score": 0.0,
            "top_name": None,
        }

    # Email before calendar — "check my E today" must not become check_calendar.
    if bool(q_tokens & _EMAIL_ANCHORS) and "check_email" in registry:
        return {
            "tools": [registry["check_email"].to_function_tool()],
            "tool_choice": {"type": "function", "name": "check_email"},
            "top_score": top_score,
            "top_name": "check_email",
        }

    # News before calendar-day force — "news today in Bangalore" used to lose to
    # check_calendar solely because "today" is a calendar day word (live log).
    if bool(q_tokens & _NEWS_WORDS) and "web_search" in registry:
        return {
            "tools": [registry["web_search"].to_function_tool()],
            "tool_choice": {"type": "function", "name": "web_search"},
            "top_score": top_score,
            "top_name": "web_search",
        }

    # Prefer personal Google calendar over in-car query_calendar.
    # Day-only follow-ups ("what about tomorrow") OK when not news/email/weather.
    competing = bool(q_tokens & (_NEWS_WORDS | _EMAIL_ANCHORS | _WEATHER_ANCHORS))
    day_followup = bool(q_tokens & _CALENDAR_DAY_WORDS) and not competing
    wants_cal = (
        bool(q_tokens & _CALENDAR_ANCHORS)
        or _has_explicit_date(query)
        or day_followup
        or bool(re.search(r"day\s+after\s+tomorrow", query.lower()))
    )
    if wants_cal and "check_calendar" in registry:
        if not (
            (_DELETE_WORDS & q_tokens)
            or (bool(_CREATE_CAL_VERBS & q_tokens) and bool((_MEETING_NOUNS | {"calendar"}) & q_tokens))
            or ("schedule" in q_tokens and bool(_MEETING_NOUNS & q_tokens))
        ):
            return {
                "tools": [registry["check_calendar"].to_function_tool()],
                "tool_choice": {"type": "function", "name": "check_calendar"},
                "top_score": top_score,
                "top_name": "check_calendar",
            }

    # Windows/sunroof: "check/status" → query; open/close → set_*.
    if bool(q_tokens & _WINDOW_WORDS) and "query_vehicle_status" in registry:
        acting = bool(q_tokens & _OPENING_ACT_WORDS)
        statusy = bool(q_tokens & _STATUS_WORDS) or "how" in q_tokens
        if statusy and not acting:
            return {
                "tools": [registry["query_vehicle_status"].to_function_tool()],
                "tool_choice": {"type": "function", "name": "query_vehicle_status"},
                "top_score": top_score,
                "top_name": "query_vehicle_status",
            }
        if acting and "set_windows" in registry:
            return {
                "tools": [registry["set_windows"].to_function_tool()],
                "tool_choice": {"type": "function", "name": "set_windows"},
                "top_score": top_score,
                "top_name": "set_windows",
            }

    # Create Drive folder before list-drive force.
    create_drive = (
        bool(_CREATE_DRIVE_VERBS & q_tokens)
        and bool(_FOLDER_WORDS & q_tokens)
        and "create_drive_folder" in registry
    )
    if create_drive:
        return {
            "tools": [registry["create_drive_folder"].to_function_tool()],
            "tool_choice": {"type": "function", "name": "create_drive_folder"},
            "top_score": top_score,
            "top_name": "create_drive_folder",
        }

    # Google Drive files (list only — create handled above).
    if bool(q_tokens & _DRIVE_WORDS) and "list_drive_files" in registry:
        return {
            "tools": [registry["list_drive_files"].to_function_tool()],
            "tool_choice": {"type": "function", "name": "list_drive_files"},
            "top_score": top_score,
            "top_name": "list_drive_files",
        }

    # "delete/cancel the reminder" — we have no delete_reminder tool; forcing
    # set_reminder made Gemma invent a confirm flow. Keep reminder tools under
    # required so the model can list/clarify without a wrong named force.
    if (_DELETE_WORDS & q_tokens) and (_REMINDER_WORDS & q_tokens):
        rem = [t for t in tools if t["name"] in {"list_reminders", "set_reminder"}]
        tools = rem or tools[:2]
        return {
            "tools": tools,
            "tool_choice": "required",
            "top_score": top_score,
            "top_name": tools[0]["name"] if tools else None,
        }

    # Delete/cancel a meeting/event/calendar item → force delete tool.
    if (_DELETE_WORDS & q_tokens) and ((_CALENDAR_ANCHORS | _MEETING_NOUNS) & q_tokens):
        winners = [t for t in tools if t["name"] == "delete_calendar_event"]
        if not winners and "delete_calendar_event" in registry:
            winners = [registry["delete_calendar_event"].to_function_tool()]
        if winners:
            return {
                "tools": winners,
                "tool_choice": {"type": "function", "name": "delete_calendar_event"},
                "top_score": top_score,
                "top_name": "delete_calendar_event",
            }

    # Create/book a meeting (not bare "what's my schedule").
    create_meeting = bool(_CREATE_CAL_VERBS & q_tokens) and bool(
        (_MEETING_NOUNS | {"calendar"}) & q_tokens
    )
    schedule_meeting = "schedule" in q_tokens and bool(_MEETING_NOUNS & q_tokens)
    if create_meeting or schedule_meeting:
        winners = [t for t in tools if t["name"] == "create_calendar_event"]
        if not winners and "create_calendar_event" in registry:
            winners = [registry["create_calendar_event"].to_function_tool()]
        if winners:
            return {
                "tools": winners,
                "tool_choice": {"type": "function", "name": "create_calendar_event"},
                "top_score": top_score,
                "top_name": "create_calendar_event",
            }

    def _anchor_ok(name: str) -> bool:
        if name == "check_email":
            return bool(q_tokens & _EMAIL_ANCHORS)
        if name == "check_calendar":
            return (
                bool(q_tokens & _CALENDAR_ANCHORS)
                or _has_explicit_date(query)
                or (
                    bool(q_tokens & _CALENDAR_DAY_WORDS)
                    and not bool(q_tokens & (_NEWS_WORDS | _EMAIL_ANCHORS | _WEATHER_ANCHORS))
                )
                or bool(re.search(r"day\s+after\s+tomorrow", query.lower()))
            )
        # Mock in-car calendar must not win over Google check_calendar.
        if name == "query_calendar":
            return "check_calendar" not in registry
        if name == "create_calendar_event":
            return bool(_CREATE_CAL_VERBS & q_tokens) or (
                "schedule" in q_tokens and bool(_MEETING_NOUNS & q_tokens)
            )
        if name == "delete_calendar_event":
            return bool(_DELETE_WORDS & q_tokens)
        # get_weather's "place" param alone matches STT garble ("what's the place"
        # for "price") — require a real weather cue before treating it as a hit.
        if name == "get_weather":
            return bool(q_tokens & _WEATHER_ANCHORS)
        if name == "create_drive_folder":
            return bool(_CREATE_DRIVE_VERBS & q_tokens) and bool(_FOLDER_WORDS & q_tokens)
        if name in {"start_research", "research_topic", "get_research_result"}:
            return _wants_research(query, q_tokens)
        return True

    # Drop check_* that only matched the verb "check" before force/margin math
    # (otherwise "check the weather" ties get_weather with check_email at 4.0).
    candidates = [t for t in tools if _anchor_ok(t["name"])] or tools
    top_name = candidates[0]["name"] if candidates else ""
    top_score = router.score(query, top_name) if top_name else 0.0
    second_score = (
        router.score(query, candidates[1]["name"]) if len(candidates) > 1 else 0.0
    )

    clear_winner = top_score >= _REQUIRED_SCORE and (top_score - second_score) >= _FORCE_MARGIN
    if clear_winner:
        winners = [t for t in candidates if t["name"] == top_name or t["name"] in pinned]
        tools = winners or candidates[:1]
        if top_name in _MULTI_CALL_TOOLS:
            tool_choice = "required"
        else:
            tool_choice = {"type": "function", "name": top_name}
    elif top_score >= _REQUIRED_SCORE:
        # Ambiguous but positive: shrink to top few positives, require a call.
        positive = [t for t in candidates if router.score(query, t["name"]) > 0][:3]
        tools = positive or candidates[:2]
        if pinned:
            for t in router.top_k(query, k=k, pinned=pinned):
                if t["name"] in pinned and all(t["name"] != x["name"] for x in tools):
                    tools.append(t)
        tool_choice = "required"
    elif pinned:
        tools = [t for t in tools if t["name"] in pinned] or tools
        tool_choice = "required"
    else:
        tools = candidates

    return {
        "tools": tools,
        "tool_choice": tool_choice,
        "top_score": top_score,
        "top_name": top_name or None,
    }
