"""Proper-noun repair must fix the misheard name without touching plain speech.

The failure it exists for: "BRICS summit" reached the search tool as "Bricks
summit", which returned a masonry event ("the Red Brick Forum in Jeddah"), and
the following turns anchored on that wrong subject.
"""
from __future__ import annotations

import pytest

from nova_hailo.stt_vocab import correct


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("It is Bricks summit.", "It is BRICS summit."),
        ("the rest tomorrow Brig's summit", "the rest tomorrow BRICS summit"),
        (
            "President Putin arrived India for the BRIX summit",
            "President Putin arrived India for the BRICS summit",
        ),
        ("which countries are in the bricks group", "which countries are in the BRICS group"),
        ("tell me about porsch cars", "tell me about Porsche cars"),
        ("the folks wagon group", "the Volkswagen group"),
    ],
)
def test_known_mishearings_are_repaired(heard: str, expected: str) -> None:
    assert correct(heard) == expected


@pytest.mark.parametrize(
    "innocent",
    [
        # "bricks" is an ordinary word -- correcting it bare would wreck these.
        "the wall is made of bricks",
        "he dropped a pile of bricks on the floor",
        "lego bricks are expensive",
        "she breaks for lunch at noon",
        "set the climate to twenty two degrees",
    ],
)
def test_plain_speech_is_untouched(innocent: str) -> None:
    """Over-correction is worse than the bug: it corrupts correct transcripts."""
    assert correct(innocent) == innocent


def test_is_idempotent() -> None:
    """Already-correct text must survive a second pass unchanged."""
    once = correct("It is Bricks summit.")
    assert correct(once) == once


def test_empty_input() -> None:
    assert correct("") == ""
