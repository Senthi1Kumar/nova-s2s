"""Conversation history must not replay a prior answer as fresh evidence.

Reproduces the live failure: a search about the new iPhone was followed by an
unrelated question about an election, and the model answered about the iPhone.
The previous spoken answer was being stored a second time as ``tool_summary``
and replayed in the *user* channel as "(Prior tool result summary: ...)", so it
read as evidence supplied for the current question.
"""
from __future__ import annotations

from nova_hailo.context_history import ConversationHistory

IPHONE_ANSWER = (
    "Apple announced the iPhone 18 Pro and a foldable duo at yesterday's event."
)
ELECTION_Q = "What's the latest on the coming election, democrats versus republicans?"


def _user_text(messages: list[dict]) -> str:
    return " ".join(m["content"] for m in messages if m["role"] == "user")


def test_prior_answer_is_not_replayed_as_tool_evidence() -> None:
    """The bleed guard: a past answer must not reappear as user-channel evidence."""
    h = ConversationHistory(max_turns=5)
    h.add("What's the newest iPhone?", IPHONE_ANSWER)

    messages = h.build_messages("You are Nova.", ELECTION_Q)

    assert not any(
        "Prior tool result summary" in m["content"] for m in messages
    ), "a prior answer is being presented to the model as tool evidence"
    assert "foldable duo" not in _user_text(messages), (
        "iPhone content leaked into the user channel of an election question"
    )


def test_duplicate_tool_summary_is_dropped() -> None:
    """Defence in depth: a summary that merely repeats the answer is not stored.

    Callers previously passed ``tool_summary=answer[:160]``, which duplicated
    the assistant turn. Keep that from silently coming back.
    """
    h = ConversationHistory(max_turns=5)
    h.add("What's the newest iPhone?", IPHONE_ANSWER, tool_summary=IPHONE_ANSWER[:160])

    messages = h.build_messages("You are Nova.", ELECTION_Q)

    assert not any("Prior tool result summary" in m["content"] for m in messages)


def test_real_followup_context_survives() -> None:
    """The fix must not blind the model to genuine follow-ups."""
    h = ConversationHistory(max_turns=5)
    h.add("What's Russia's GDP?", "Russia's GDP is about 2 trillion dollars.")

    messages = h.build_messages("You are Nova.", "And since the invasion until now?")

    assistant = " ".join(m["content"] for m in messages if m["role"] == "assistant")
    assert "2 trillion" in assistant, "the answer that names the subject was lost"


def test_distinct_tool_summary_is_still_carried() -> None:
    """A genuine tool summary -- not a copy of the answer -- still reaches the model."""
    h = ConversationHistory(max_turns=5)
    h.add(
        "Check my calendar",
        "You're free this afternoon.",
        tool_summary="calendar: 0 events between 12:00 and 18:00",
    )

    messages = h.build_messages("You are Nova.", "Book something then.")

    assert any("0 events between" in m["content"] for m in messages)


def test_current_datetime_rides_with_the_question() -> None:
    """Stable facts are supplied, not inferred from the transcript.

    Live failure: asked for a calendar date, Nova said "the 11th of September",
    was told it was wrong, and then said "the 12th of September" -- it had no
    idea what day it actually was.
    """
    from datetime import datetime

    h = ConversationHistory(max_turns=5)
    messages = h.build_messages(
        "You are Nova.", "what's the date?", now=datetime(2026, 9, 11, 10, 6)
    )
    last = messages[-1]["content"]
    assert "Friday" in last and "11 September 2026" in last
    # The question stays last so it is what the model answers.
    assert last.rstrip().endswith("what's the date?")


def test_state_line_is_not_in_the_cacheable_prefix() -> None:
    """The changing value must not sit in the system prompt.

    Groq caches on an exact prefix match, so a timestamp in the system message
    would miss the cache on every single turn.
    """
    from datetime import datetime

    h = ConversationHistory(max_turns=5)
    messages = h.build_messages("You are Nova.", "hi", now=datetime(2026, 9, 11, 10, 6))
    assert messages[0]["role"] == "system"
    assert "September" not in messages[0]["content"]
