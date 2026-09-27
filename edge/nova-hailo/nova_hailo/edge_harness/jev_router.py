"""Semantic tool router via one TypeSafe Jev (System One) request.

Callers: ``nova_hailo.tools.oem_tools.OemToolGateway`` when ``tools.router`` is
``jev``. Returns canonical ``RoutedIntent`` values for ``ToolBroker``. On a
missing key, timeout, or SDK error, returns ``None`` so the gateway can fall
back to ``edge_harness.router.route``.

``typesafe-sdk`` is an optional extra. It is imported only inside the live
call so regex-only installs and pytest never require the package.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.router import (
    _smalltalk_speak,
    _unavailable_cap_speak,
    calendar_day_slot,
)
from nova_hailo.edge_harness.task_state import TaskState
from nova_hailo.edge_harness.types import Intent, RoutedIntent

logger = logging.getLogger(__name__)

# Stable execution order. deep_research is exclusive with any other winner.
_TOOL_ORDER: tuple[Intent, ...] = (
    Intent.CHECK_CALENDAR,
    Intent.CHECK_EMAIL,
    Intent.LIST_DRIVE_FILES,
    Intent.DEEP_RESEARCH,
    Intent.WEB_SEARCH,
)
_CALENDAR_DAYS = frozenset({"today", "tomorrow", "day_after_tomorrow", "week"})
_SMALLTALK_SPEAK = "Hello. How can I help?"
_DISABLED_SPEAK = "I can't do that yet."


def _noul(question: str, yes: str, no: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": {"question": question, "inspect": "`utterance`"},
        "criteria": {"true": yes, "false": no},
    }


def build_questions(profile: CapabilityProfile) -> dict[str, Any]:
    """One Noul per enabled tool, plus smalltalk, disabled-tool, and calendar day."""
    questions: dict[str, Any] = {
        "smalltalk": _noul(
            "Is `utterance` only a greeting, thanks, goodbye, or chat with no tool request?",
            "Greeting, thanks, goodbye, a joke, or chitchat that does not ask for a tool",
            "Asks to look something up, check calendar, mail, files, or research",
        ),
        "asks_disabled_tool": _noul(
            "Does `utterance` ask for a capability that is not in `enabled_tools`?",
            "Asks for a tool or vehicle action absent from `enabled_tools`",
            "Asks only for a listed tool, or asks for nothing",
        ),
    }
    specs = {
        Intent.CHECK_CALENDAR: (
            "Does `utterance` ask to check the calendar, schedule, or meetings?",
            "Asks what is on the calendar or when a meeting is",
            "Does not ask about the calendar",
        ),
        Intent.CHECK_EMAIL: (
            "Does `utterance` ask to check email or the inbox?",
            "Asks to read, check, or summarize mail",
            "Does not ask about email",
        ),
        Intent.LIST_DRIVE_FILES: (
            "Does `utterance` ask to list or find files in Drive?",
            "Asks what files or folders are in Drive",
            "Does not ask about Drive files",
        ),
        Intent.DEEP_RESEARCH: (
            "Does `utterance` ask for research, a deep dive, or an investigation?",
            "Asks to research or dig into a topic",
            "A quick lookup, news, or weather ask, not a research job",
        ),
        Intent.WEB_SEARCH: (
            "Does `utterance` ask for a web lookup: news, weather, stocks, or a fact to search?",
            "Needs a live web lookup",
            "Calendar, mail, Drive, research, or pure chat",
        ),
    }
    for intent in _TOOL_ORDER:
        if not profile.allows(intent):
            continue
        question, yes, no = specs[intent]
        questions[intent.value] = _noul(question, yes, no)
    if profile.allows(Intent.CHECK_CALENDAR):
        questions["calendar_day"] = {
            "type": "choice",
            "instructions": {
                "question": "Which day window does the calendar part of `utterance` ask about?",
                "focus": "Ignore any non-calendar clause.",
            },
            "criteria": {
                "today": "today, tonight, or this morning",
                "tomorrow": "tomorrow",
                "day_after_tomorrow": "the day after tomorrow",
                "week": "this week, or no specific day was named",
            },
        }
    return questions


def build_state(query: str, profile: CapabilityProfile, task_state: TaskState | None) -> dict[str, Any]:
    state = task_state or TaskState()
    return {
        "utterance": query,
        "enabled_tools": sorted(profile.enabled),
        "last_tool": state.last_tool,
        "last_query": state.last_query,
    }


def _answer_map(result: Any) -> dict[str, Any]:
    answers = getattr(result, "answers", None)
    if answers is None and isinstance(result, dict):
        answers = result.get("answers")
    if answers is None:
        return {}
    if isinstance(answers, dict):
        return answers
    try:
        return dict(answers)
    except (TypeError, ValueError):
        return {}


def _noul_of(answers: dict[str, Any], key: str) -> float:
    raw = answers.get(key)
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        val = raw.get("noul")
    else:
        val = getattr(raw, "noul", None)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _choice_of(answers: dict[str, Any], key: str) -> str | None:
    raw = answers.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        val = raw.get("choice")
    else:
        val = getattr(raw, "choice", None)
    if val is None:
        return None
    return str(val)


def compose_jev_answers(
    result: Any,
    query: str,
    profile: CapabilityProfile,
    *,
    noul_min: float = 0.65,
    max_actions: int = 2,
) -> list[RoutedIntent] | None:
    """Turn one System One response into one or two routed intents.

    ``None`` means nothing cleared ``noul_min`` (the 0.55 unsure band included):
    the caller should use the regex router.
    """
    answers = _answer_map(result)
    scores = {
        intent: _noul_of(answers, intent.value)
        for intent in _TOOL_ORDER
        if profile.allows(intent)
    }
    selected = [intent for intent in _TOOL_ORDER if scores.get(intent, 0.0) >= noul_min]
    if Intent.DEEP_RESEARCH in selected and len(selected) > 1:
        selected = [Intent.DEEP_RESEARCH]
    selected = selected[: max(1, max_actions)]
    if selected:
        day = _choice_of(answers, "calendar_day")
        if day not in _CALENDAR_DAYS:
            day = calendar_day_slot(query)
        routed: list[RoutedIntent] = []
        for intent in selected:
            slots: dict[str, Any] = {}
            if intent is Intent.CHECK_CALENDAR:
                slots["day"] = day
            routed.append(RoutedIntent(intent, query, slots))
        return routed

    if _noul_of(answers, "smalltalk") >= noul_min:
        speak = _smalltalk_speak(query) or _SMALLTALK_SPEAK
        return [RoutedIntent(Intent.SMALLTALK, query, {"speak": speak})]

    if _noul_of(answers, "asks_disabled_tool") >= noul_min:
        speak = _unavailable_cap_speak(query, profile.enabled) or _DISABLED_SPEAK
        return [RoutedIntent(Intent.CAPABILITY_UNAVAILABLE, query, {"speak": speak})]

    return None


def route_with_jev(
    query: str,
    profile: CapabilityProfile,
    task_state: TaskState | None = None,
    *,
    client: Any = None,
    timeout_sec: float = 0.45,
    noul_min: float = 0.65,
    max_actions: int = 2,
) -> list[RoutedIntent] | None:
    """One System One call. ``None`` on unsure, missing key, timeout, or error."""
    q = (query or "").strip()
    if not q:
        return None
    if client is None and not os.environ.get("TYPESAFE_API_KEY", "").strip():
        logger.info("jev_router:fallback")
        return None
    state = build_state(q, profile, task_state)
    questions = build_questions(profile)
    try:
        if client is None:
            # Optional extra: keep the import off the regex/test import path.
            from typesafe_sdk import RetryPolicy, TypeSafeClient

            retry = RetryPolicy(max_retries=0)
            with TypeSafeClient(timeout=timeout_sec, retry=retry) as live:
                result = live.system_one(
                    state, questions, timeout=timeout_sec, retry=retry
                )
        else:
            result = client.system_one(state, questions, timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001 — any SDK/network failure falls back
        logger.info("jev_router:fallback %s", type(exc).__name__)
        return None
    routed = compose_jev_answers(
        result, q, profile, noul_min=noul_min, max_actions=max_actions
    )
    if routed is None:
        logger.info("jev_router:unsure")
    return routed
