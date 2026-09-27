"""Cloud tool follow-up: Groq picks the tool; host chooses what to voice.

Numeric/stock results already have a spoken line (needs_summary False).
News/calendar still let Groq rewrite. An empty rewrite must not throw away
the tool speak -- that was the AMD/Intel live miss.
"""
from __future__ import annotations

from nova_hailo.edge_harness.cloud_tool_speak import (
    CLOUD_EMPTY_REWRITE_SPEAK,
    resolve_cloud_followup_speak,
    tool_result_is_final,
)
from nova_hailo.edge_harness.openai_tools import openai_tools_for_profile
from nova_hailo.edge_harness.policy import CapabilityProfile


def test_numeric_stock_result_is_final():
    out = {
        "ok": True,
        "name": "web_search",
        "speak": "Search results say AMD $161.20 and Intel $24.50.",
        "result": {"numeric": True, "needs_summary": False, "evidence": "AMD $161.20"},
    }
    assert tool_result_is_final(out) is True


def test_news_result_is_not_final():
    out = {
        "ok": True,
        "name": "web_search",
        "speak": "A gym trainer's murder in Aya Nagar.",
        "result": {"numeric": False, "needs_summary": True, "evidence": "Delhi"},
    }
    assert tool_result_is_final(out) is False


def test_calendar_result_is_not_final():
    out = {
        "ok": True,
        "name": "check_calendar",
        "speak": "You have one event today at ten AM.",
        "result": {"events": 1},
    }
    assert tool_result_is_final(out) is False


def test_empty_or_missing_speak_is_not_final():
    assert tool_result_is_final(None) is False
    assert tool_result_is_final({"result": {"numeric": True}, "speak": ""}) is False
    assert tool_result_is_final({"result": {"numeric": True}}) is False


def test_numeric_skips_groq_rewrite_even_if_model_said_something():
    tool = "Search results say AMD $161.20 and Intel $24.50."
    assert (
        resolve_cloud_followup_speak(
            llm_rewrite="I'm not sure about those tickers.",
            tool_speak=tool,
            tool_is_final=True,
        )
        == tool
    )


def test_empty_groq_rewrite_falls_back_to_tool_speak():
    """Live session 20260914_210528_3209 turns 10-11: generated_tokens=0."""
    tool = "Search results say AMD $161.20 and Intel $24.50."
    assert (
        resolve_cloud_followup_speak(
            llm_rewrite="",
            tool_speak=tool,
            tool_is_final=False,
        )
        == tool
    )


def test_news_keeps_groq_rewrite_when_it_has_content():
    rewrite = "The top local story is a gym trainer's murder in Aya Nagar."
    assert (
        resolve_cloud_followup_speak(
            llm_rewrite=rewrite,
            tool_speak="A gym trainer's murder in Aya Nagar.",
            tool_is_final=False,
        )
        == rewrite
    )


def test_both_empty_uses_generic_closed_line():
    assert (
        resolve_cloud_followup_speak(llm_rewrite="", tool_speak="")
        == CLOUD_EMPTY_REWRITE_SPEAK
    )
    assert CLOUD_EMPTY_REWRITE_SPEAK == "I couldn't get that just now."


def test_groq_toolbox_has_search_not_clock():
    """Caveat: Groq picks from the OEM toolbox. Clock stays host-routed."""
    tools = openai_tools_for_profile(
        CapabilityProfile.from_list(
            ["web_search", "check_calendar", "check_email", "list_drive_files"]
        )
    )
    names = {t["function"]["name"] for t in tools}
    assert "web_search" in names
    assert "check_calendar" in names
    assert "get_datetime" not in names


def test_numeric_miss_is_not_final():
    # A price lookup that found nothing must not short-circuit the reply:
    # voicing it verbatim ignored follow-ups like "Why is it?" and repeated the
    # same miss line for every question in the thread.
    out = {
        "ok": True,
        "name": "web_search",
        "speak": "I couldn't find a reliable number for that.",
        "result": {"numeric": True, "numeric_miss": True, "needs_summary": False,
                   "evidence": "Nvidia unveils new chips"},
    }
    assert tool_result_is_final(out) is False


def test_numeric_miss_still_voiced_when_groq_returns_nothing():
    # Groq round 2 can come back empty (gpt-oss 0-token); the miss line is
    # still better than a generic "I didn't catch that".
    assert resolve_cloud_followup_speak(
        llm_rewrite="",
        tool_speak="I couldn't find a reliable number for that.",
        tool_is_final=False,
    ) == "I couldn't find a reliable number for that."


# ---- after a numeric miss the model must not invent the number -------------

from nova_hailo.edge_harness.cloud_tool_speak import (  # noqa: E402
    guard_numeric_miss_text,
    is_numeric_miss,
    states_amount,
    tool_message_content,
)

_MISS = {
    "ok": True,
    "name": "web_search",
    "status": "success",
    "speak": "I couldn't find a reliable number for that.",
    "result": {"numeric": True, "numeric_miss": True, "evidence": "Nvidia unveils chips"},
}
_HIT = {
    "ok": True,
    "name": "web_search",
    "status": "success",
    "speak": "Search results say NVIDIA $182.40.",
    "result": {"numeric": True, "numeric_miss": False, "evidence": "NVDA $182.40"},
}


def test_states_amount_catches_prices_and_percentages():
    for text in ("It's trading at $130.", "around 182.40 dollars", "down 2.5% today",
                 "up 3 percent", "about ₹1,200", "€45 a share", "roughly 90 USD"):
        assert states_amount(text), text
    for text in ("I couldn't find a current price for NVIDIA.",
                 "Search snippets rarely carry live quotes.", "In 2026 it launched Blackwell."):
        assert not states_amount(text), text


def test_is_numeric_miss():
    assert is_numeric_miss(_MISS) is True
    assert is_numeric_miss(_HIT) is False
    assert is_numeric_miss({"ok": True, "speak": "news", "result": {"numeric": False}}) is False
    assert is_numeric_miss(None) is False


def test_tool_message_tells_model_not_to_state_numbers_after_a_miss():
    miss = tool_message_content(_MISS)
    assert miss["speak"] == _MISS["speak"]
    assert "do not state" in miss["note"].lower()
    assert "note" not in tool_message_content(_HIT)


def test_guard_replaces_invented_amount_after_miss():
    assert guard_numeric_miss_text(
        "NVIDIA is trading around $130.", forbid=True, miss_speak=_MISS["speak"]
    ) == _MISS["speak"]


def test_guard_keeps_honest_text_and_ignores_normal_turns():
    honest = "I couldn't find a live NVIDIA price; a finance app will have it."
    assert guard_numeric_miss_text(honest, forbid=True, miss_speak="x") == honest
    assert guard_numeric_miss_text("It rose 3% today.", forbid=False, miss_speak="x") == (
        "It rose 3% today."
    )


def test_search_results_tell_the_model_to_answer_from_them():
    # Seen on the Pi: the search returned current headlines, and the model
    # still said "my info is a bit fuzzy on today's specifics".
    news = {
        "ok": True, "name": "web_search", "status": "success",
        "speak": "OpenAI says dozens of third parties were affected by agents...",
        "result": {"numeric": False, "needs_summary": True, "evidence": "OpenAI says..."},
    }
    content = tool_message_content(news)
    assert "answer" in content["note"].lower()
    assert "current" in content["note"].lower()


def test_failed_tool_gets_no_answer_from_results_note():
    failed = {"ok": False, "name": "web_search", "status": "unavailable",
              "speak": "I can't reach that service right now.", "result": {}}
    assert "note" not in tool_message_content(failed)
