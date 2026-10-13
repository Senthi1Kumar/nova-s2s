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
    user = " ".join(m["content"] for m in messages if m["role"] == "user")
    assert "IST" in user
    assert "10:06" in user


# ---- a refused turn drops its reply, not its question ------------------------

SPACEX_Q = "What is the stock price of SpaceX right now?"
SPACEX_REFUSAL = "I'm not sure there's a public stock price for SpaceX right now."


def test_question_behind_a_refusal_is_carried_to_the_next_turn() -> None:
    """"Why is it?" after a "not sure" reply asked the user to clarify,
    because the whole refused turn -- question included -- was dropped."""
    h = ConversationHistory(max_turns=5)
    h.add(SPACEX_Q, SPACEX_REFUSAL)

    messages = h.build_messages("You are Nova.", "Why is it?")

    current = messages[-1]["content"]
    assert SPACEX_Q in current
    assert current.rstrip().endswith("Why is it?")
    # The refusal itself still never enters the prompt (repetition-lock guard).
    assert not any(SPACEX_REFUSAL in m["content"] for m in messages)


def test_carried_question_is_dropped_once_a_turn_is_stored() -> None:
    h = ConversationHistory(max_turns=5)
    h.add(SPACEX_Q, SPACEX_REFUSAL)
    h.add("Why is it?", "SpaceX is privately held, so it has no public share price.")

    messages = h.build_messages("You are Nova.", "What's the weather in Delhi?")

    assert not any(
        SPACEX_Q in m["content"] and m["role"] == "user" and m is messages[-1]
        for m in messages
    )


def test_clear_drops_the_carried_question() -> None:
    h = ConversationHistory(max_turns=5)
    h.add(SPACEX_Q, SPACEX_REFUSAL)
    h.clear()

    assert SPACEX_Q not in h.build_messages("You are Nova.", "Why is it?")[-1]["content"]


def test_an_untrusted_turn_never_leaves_an_unanswered_question():
    from nova_hailo.context_history import ConversationHistory

    h = ConversationHistory(max_turns=3)
    h.add("navigating to work about nine minutes", "I'm sorry, I didn't catch that.", trusted=False)
    msgs = h.build_messages("sys", "what's the weather")
    assert "Earlier question" not in msgs[-1]["content"]


def test_an_unanswered_question_is_offered_once_and_expires():
    from nova_hailo.context_history import ConversationHistory

    t = [0.0]
    h = ConversationHistory(max_turns=3, unanswered_ttl_s=60, clock=lambda: t[0])
    h.add("why is it", "I'm sorry, I didn't catch that.")
    assert "Earlier question" in h.build_messages("sys", "search for it")[-1]["content"]
    assert "Earlier question" not in h.build_messages("sys", "and now")[-1]["content"]
    h.add("why is it", "I'm sorry, I didn't catch that.")
    t[0] = 61.0
    assert "Earlier question" not in h.build_messages("sys", "search for it")[-1]["content"]
