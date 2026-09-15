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
