"""Host-owned speak length: short by default, longer when the task is ongoing."""
from __future__ import annotations

from dataclasses import dataclass

from nova_hailo.edge_harness.task_state import TaskState

_TOOL_FAMILY = frozenset(
    {
        "web_search",
        "deep_research",
        "check_calendar",
        "check_email",
        "list_drive_files",
    }
)


@dataclass(frozen=True)
class SpeakBudget:
    max_tokens: int
    instruction: str
    voice_one_sentence: bool
    depth: str  # short | followup | grounded


def speak_budget(
    *,
    history_turns: int = 0,
    state: TaskState | None = None,
    tool_name: str | None = None,
    backend: str = "hailo",
) -> SpeakBudget:
    """Pick max_tokens + prompt line from conversation depth.

    ``backend`` is ``openrouter`` or anything else (Hailo HEF). Hailo stays
    tight because the compiled context and decode rate are small.
    """
    cloud = backend == "openrouter"
    tool = (tool_name or "") in _TOOL_FAMILY
    turns = max(0, int(history_turns))
    if state is not None:
        if state.last_tool:
            turns = max(turns, 1)
        if state.intent and state.intent in _TOOL_FAMILY:
            tool = True

    if tool:
        return SpeakBudget(
            max_tokens=220 if cloud else 96,
            instruction=(
                "Answer in 3 to 5 complete sentences using only the tool result. "
                "Do not invent facts or mention tools."
            ),
            voice_one_sentence=False,
            depth="grounded",
        )
    if turns >= 2:
        return SpeakBudget(
            max_tokens=160 if cloud else 64,
            instruction=(
                "The conversation is ongoing. Answer in 2 to 4 complete sentences. "
                "Stay grounded in what was already said."
            ),
            voice_one_sentence=False,
            depth="followup",
        )
    return SpeakBudget(
        max_tokens=80 if cloud else 48,
        instruction="Answer in one or two short complete sentences. Be direct.",
        voice_one_sentence=True,
        depth="short",
    )
