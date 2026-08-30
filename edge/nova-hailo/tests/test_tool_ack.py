"""Tool-dispatch acknowledgement: per-tool line choice + no-double-speak guard.

Speaking a short filler ("Let me check that.") the instant a tool call is
picked -- instead of staying silent through tool execution and a second LLM
round -- is what moves first audio on a tool turn from ~3.4s to ~900ms. Both
pieces here are pure (no TTS, no hardware, no nova_hailo.pipeline import --
that module imports hailo_platform at module level, unavailable off-Pi), so
they're testable directly.
"""
from __future__ import annotations

import random

from nova_hailo.edge_harness.tool_ack import (
    _DEFAULT_ACK_LINES,
    _TOOL_ACK_LINES,
    choose_tool_ack,
    should_speak_tool_ack,
)

_KNOWN_TOOLS = (
    "web_search",
    "check_calendar",
    "check_email",
    "list_drive_files",
    "deep_research",
)


def test_every_known_tool_has_its_own_lines():
    for tool in _KNOWN_TOOLS:
        assert tool in _TOOL_ACK_LINES
        assert len(_TOOL_ACK_LINES[tool]) >= 2  # room to vary, not one canned line


def test_every_line_is_under_five_words():
    all_lines = list(_DEFAULT_ACK_LINES)
    for lines in _TOOL_ACK_LINES.values():
        all_lines.extend(lines)
    for line in all_lines:
        word_count = len(line.split())
        assert word_count <= 5, f"{line!r} is {word_count} words, over the ~5 word budget"


def test_unknown_tool_falls_back_to_generic_lines():
    line = choose_tool_ack("some_future_mcp_tool", rng=random.Random(1))
    assert line in _DEFAULT_ACK_LINES


def test_empty_or_none_tool_name_falls_back_to_generic_lines():
    assert choose_tool_ack(None, rng=random.Random(1)) in _DEFAULT_ACK_LINES
    assert choose_tool_ack("", rng=random.Random(1)) in _DEFAULT_ACK_LINES


def test_tool_name_matching_is_case_and_whitespace_insensitive():
    line = choose_tool_ack(" Check_Calendar ", rng=random.Random(1))
    assert line in _TOOL_ACK_LINES["check_calendar"]


def test_deep_research_line_sets_a_longer_wait_expectation():
    """deep_research kicks off an async multi-step job, not a quick fetch --
    its lines must be honest about that instead of reusing the quick-lookup
    phrasing every other tool gets."""
    for line in _TOOL_ACK_LINES["deep_research"]:
        assert line not in _TOOL_ACK_LINES["web_search"]


def test_choice_is_deterministic_under_a_fixed_seed():
    a = choose_tool_ack("web_search", rng=random.Random(42))
    b = choose_tool_ack("web_search", rng=random.Random(42))
    assert a == b


def test_choice_varies_across_the_table_given_enough_draws():
    """Not a single canned recording: over many draws with different seeds,
    more than one variant for the tool must come up."""
    seen = {choose_tool_ack("web_search", rng=random.Random(i)) for i in range(50)}
    assert len(seen) > 1
    assert seen.issubset(set(_TOOL_ACK_LINES["web_search"]))


def test_default_rng_is_the_shared_random_module():
    """No rng passed -> non-deterministic module random, not a fixed line."""
    result = choose_tool_ack("web_search")
    assert result in _TOOL_ACK_LINES["web_search"]


# --- should_speak_tool_ack: the no-double-speak / no-second-round guard ---


def test_speaks_on_the_first_round_when_nothing_spoken_yet():
    assert should_speak_tool_ack(enabled=True, round_index=0, spoken_parts=[]) is True


def test_does_not_speak_when_disabled_by_config():
    assert should_speak_tool_ack(enabled=False, round_index=0, spoken_parts=[]) is False


def test_does_not_speak_if_the_model_already_streamed_content_this_round():
    """The main failure mode this guards against: the model streamed a
    preamble ('Sure, let me check —') via token_cb before also calling a
    tool in the same round. Speaking a filler on top would double-speak."""
    assert (
        should_speak_tool_ack(enabled=True, round_index=0, spoken_parts=["Sure,"])
        is False
    )


def test_does_not_speak_on_the_second_round_after_a_tool_result():
    """Only the round that first dispatches a tool gets an ack -- a
    follow-up round (after a tool result came back), even if it also
    produces new tool calls, must never get a second one."""
    assert should_speak_tool_ack(enabled=True, round_index=1, spoken_parts=[]) is False
    assert should_speak_tool_ack(enabled=True, round_index=2, spoken_parts=[]) is False
