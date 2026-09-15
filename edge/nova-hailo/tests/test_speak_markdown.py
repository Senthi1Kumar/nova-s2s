"""Markdown must never reach the speech synthesizer.

Live turns spoke '**Paper title:** "Hexagon-MLIR...' with the asterisks and
curly quotes intact, because sanitize_for_tts only stripped tool JSON.
"""
from __future__ import annotations

import pytest

from nova_hailo.edge_harness.speak_sanitize import (
    has_filler,
    sanitize_for_tts,
    strip_markdown,
)

# Verbatim from session 20260911_111115_3123, turn 30.
LIVE_REPLY = (
    '**Paper title:** “Hexagon‑MLIR. An AI Compilation Stack for '
    "Qualcomm’s Neural Processing Units”"
)


def test_the_live_failure_is_fixed() -> None:
    spoken = sanitize_for_tts(LIVE_REPLY)
    assert "*" not in spoken
    assert "‑" not in spoken, "non-breaking hyphen confuses Piper"
    assert "“" not in spoken and "”" not in spoken
    assert "Paper title:" in spoken
    assert "Hexagon-MLIR" in spoken


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**bold**", "bold"),
        ("*italic*", "italic"),
        ("***both***", "both"),
        ("__strong__", "strong"),
        ("_em_", "em"),
        ("`code`", "code"),
        ("# Heading", "Heading"),
        ("### Deep heading", "Deep heading"),
        ("> quoted line", "quoted line"),
        ("[Exa search](https://exa.ai)", "Exa search"),
        ("![a diagram](x.png)", "a diagram"),
        ("- first item", "first item"),
        ("1. numbered", "numbered"),
        ("2) paren numbered", "paren numbered"),
    ],
)
def test_markdown_constructs_become_plain_prose(raw, expected) -> None:
    assert strip_markdown(raw).strip() == expected


def test_bullets_do_not_fuse_onto_the_previous_line() -> None:
    """A deleted marker must leave a boundary, or words run together."""
    spoken = sanitize_for_tts("Findings:\n- alpha\n- beta")
    assert "alpha" in spoken and "beta" in spoken
    assert "Findings:alpha" not in spoken
    assert "alphabeta" not in spoken


def test_code_fences_are_dropped_entirely() -> None:
    spoken = sanitize_for_tts("Here it is:\n```python\nprint('hi')\n```\nThat's all.")
    assert "print" not in spoken
    assert "Here it is:" in spoken and "That's all." in spoken


def test_a_bolded_link_keeps_its_label() -> None:
    assert strip_markdown("**[Nova docs](https://x.dev)**").strip() == "Nova docs"


def test_plain_speech_is_untouched() -> None:
    """The common case must not be damaged by the markdown pass."""
    plain = "Yeah, the meeting is at 3 p.m. and it's in room 2."
    assert sanitize_for_tts(plain) == plain


def test_fillers_survive_markdown_stripping() -> None:
    spoken = sanitize_for_tts("Um, **yes** - the light is on.")
    assert has_filler(spoken)
    assert "*" not in spoken


def test_arithmetic_asterisks_are_not_emphasis() -> None:
    """A lone asterisk is not a markdown marker and must not eat text."""
    assert "2 * 3" in strip_markdown("The answer is 2 * 3 equals six.")


def test_empty_and_markdown_only_input_hit_the_fallback() -> None:
    assert sanitize_for_tts("", fallback="I'm here.") == "I'm here."
    assert sanitize_for_tts("***", fallback="I'm here.") == "I'm here."
    assert sanitize_for_tts("---", fallback="I'm here.") == "I'm here."
