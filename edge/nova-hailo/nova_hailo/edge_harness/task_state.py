"""Host-owned compact task state. Hailo KV is never the source of truth."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Intents that are "the same task family" for follow-ups / search-again.
_TOOL_FAMILY = frozenset(
    {
        "web_search",
        "deep_research",
        "check_calendar",
        "check_email",
        "list_drive_files",
    }
)
_CHATTY = frozenset(
    {"identity", "capability", "smalltalk", "capability_unavailable"}
)


@dataclass
class TaskState:
    intent: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    last_tool: str | None = None
    last_query: str | None = None
    last_compact_result: str | None = None
    clarify_count: int = 0
    retry_count: int = 0
    completed: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "slots": dict(self.slots),
            "last_tool": self.last_tool,
            "last_query": self.last_query,
            "last_compact_result": self.last_compact_result,
            "clarify_count": self.clarify_count,
            "retry_count": self.retry_count,
            "completed": self.completed,
        }


def is_same_task(prev: TaskState, new_intent: str | None, *, search_again: bool) -> bool:
    """True when this turn continues the host task (follow-up / search-again)."""
    if search_again:
        return bool(prev.last_tool or prev.last_query)
    if not new_intent or not prev.intent:
        return False
    if new_intent in _CHATTY:
        return False
    if prev.intent == new_intent:
        return True
    return new_intent in _TOOL_FAMILY and prev.intent in _TOOL_FAMILY


def apply_routed_turn(
    prev: TaskState,
    *,
    user_text: str,
    new_intent: str | None,
    slots: dict[str, Any] | None = None,
    search_again: bool = False,
    compact_result: str | None = None,
    tool_ok: bool | None = None,
) -> tuple[TaskState, bool]:
    """Apply a routed (or search-again) turn.

    Returns (next_state, should_clear_llm).
    Clear when the request is independent or the previous task completed.
    Keep KV only for same-task clarification / search-again / same intent.
    """
    same = is_same_task(prev, new_intent, search_again=search_again)
    clear = (not same) or prev.completed
    if search_again and (prev.last_tool or prev.last_query):
        nxt = replace(
            prev,
            intent=prev.last_tool or prev.intent,
            last_query=prev.last_query or user_text,
            retry_count=prev.retry_count + 1,
            completed=False,
            clarify_count=0,
        )
        if compact_result is not None:
            nxt.last_compact_result = compact_result[:240]
        if tool_ok is True:
            nxt.completed = True
        return nxt, False  # same task — do not clear

    if new_intent is None:
        return prev, False

    tool_family = new_intent in _TOOL_FAMILY
    success = tool_ok is True
    nxt = TaskState(
        intent=new_intent,
        slots=dict(slots or {}),
        last_tool=new_intent if (tool_family and success) else prev.last_tool,
        last_query=user_text if (tool_family and success) else prev.last_query,
        last_compact_result=(
            compact_result[:240]
            if compact_result and success
            else (prev.last_compact_result if same else None)
        ),
        clarify_count=0,
        retry_count=0,
        completed=bool(tool_family and success),
    )
    if new_intent in _CHATTY:
        nxt.last_tool = prev.last_tool
        nxt.last_query = prev.last_query
        nxt.last_compact_result = prev.last_compact_result
        nxt.completed = prev.completed
        clear = False
    return nxt, clear


def record_clarify(state: TaskState) -> TaskState:
    return replace(state, clarify_count=state.clarify_count + 1, completed=False)
