"""first_complete_clause: intra-sentence split for the turn's first unit only.

min_chars is a floor, not a target -- it cannot create a sentence boundary
that isn't there. extra_boundaries lets the caller (pipeline.py, gated by
first_chunk_intra_split and only before the first chunk of a turn is sent)
also split on comma/semicolon/colon/em-dash/en-dash, so the first spoken unit
is short without ever losing or duplicating text.

Mirrors nova_hailo.pipeline._FIRST_CHUNK_EXTRA_BOUNDARIES as a plain string
literal rather than importing it -- pipeline.py imports hailo_platform at
module level, which isn't installed on the laptop.
"""
from __future__ import annotations

import re

from nova_hailo.response_contract import first_complete_clause, validate_spoken_unit

# Comma, semicolon, colon, em-dash (U+2014), en-dash (U+2013). Deliberately
# excludes the ASCII hyphen (U+002D): it also appears inside compound words
# ("self-driving"), where it is not a clause boundary.
EXTRA = ",;:—–"


def _split_all(text: str, *, min_chars: int, extra_boundaries: str = "") -> list[str]:
    """Mimic pipeline.py's loop: repeatedly split, then flush the remainder."""
    pieces: list[str] = []
    buf = text
    while True:
        clause, rest = first_complete_clause(
            buf, min_chars=min_chars, extra_boundaries=extra_boundaries
        )
        if clause is None:
            break
        pieces.append(clause)
        buf = rest
    remainder = buf.strip()
    if remainder:
        pieces.append(remainder)
    return pieces


def _normalize(text: str) -> str:
    return " ".join(text.split())


def test_default_behaviour_unchanged_when_extra_boundaries_empty():
    """No extra_boundaries arg == today's sentence-only splitting."""
    text = "I'm doing well, thanks for asking."
    assert first_complete_clause(text, min_chars=12) == (text, "")
    # Explicit empty string is identical to omitting the argument.
    assert first_complete_clause(text, min_chars=12, extra_boundaries="") == (text, "")


def test_comma_split_when_enabled_and_min_chars_met():
    text = "I'm doing well, thanks for asking."
    clause, rest = first_complete_clause(text, min_chars=12, extra_boundaries=EXTRA)
    assert clause == "I'm doing well,"
    assert rest == " thanks for asking."


def test_no_split_before_min_chars_even_with_extra_boundaries():
    """The floor still applies: a comma at index < min_chars must not split."""
    text = "I'm doing well, thanks for asking."
    # The comma sits at index 14 (i+1 == 15); a floor of 24 must skip it and
    # fall through to the next boundary that clears the floor -- the final
    # period -- exactly like the pre-fix behaviour.
    clause, rest = first_complete_clause(text, min_chars=24, extra_boundaries=EXTRA)
    assert clause == text
    assert rest == ""


def test_no_text_lost_or_duplicated_short_floor():
    text = "I'm doing well, thanks for asking. Anything else I can help with?"
    pieces = _split_all(text, min_chars=12, extra_boundaries=EXTRA)
    assert len(pieces) > 1  # actually split into multiple short units
    assert _normalize(" ".join(pieces)) == _normalize(text)


def test_no_text_lost_or_duplicated_default_floor():
    text = "I'm doing well, thanks for asking. Anything else I can help with?"
    pieces = _split_all(text, min_chars=24)
    assert _normalize(" ".join(pieces)) == _normalize(text)


def test_no_text_lost_with_semicolon_colon_and_dash():
    text = "Sure thing; let me check: it's on the way — should arrive soon."
    pieces = _split_all(text, min_chars=8, extra_boundaries=EXTRA)
    assert _normalize(" ".join(pieces)) == _normalize(text)


def test_ascii_hyphen_in_compound_word_is_not_a_boundary():
    """A hyphen inside a compound word is not a clause boundary. Splitting on
    it truncates mid-word, e.g. "self-" -- must return either the full
    sentence or a split at a legitimate boundary, never a chunk ending in the
    ASCII hyphen."""
    examples = [
        "I can help with self-driving questions about your car.",
        "This is a well-known issue, so let me check.",
        "It is a state-of-the-art system for sure.",
        "Your check-in time is at noon today.",
    ]
    for text in examples:
        clause, rest = first_complete_clause(text, min_chars=12, extra_boundaries=EXTRA)
        assert clause is not None, f"no clause at all for {text!r}"
        assert not clause.endswith("-"), (
            f"{text!r} split mid-compound-word: clause={clause!r}"
        )
        # No text lost/duplicated for these specific inputs either.
        assert _normalize(clause + rest) == _normalize(text)


def test_em_dash_and_en_dash_still_split():
    """Both Unicode dashes ARE genuine clause boundaries and must still split."""
    em_dash_text = "I am not sure I caught that — could you repeat it?"
    en_dash_text = "I am not sure I caught that – could you repeat it?"
    em_clause, em_rest = first_complete_clause(em_dash_text, min_chars=8, extra_boundaries=EXTRA)
    en_clause, en_rest = first_complete_clause(en_dash_text, min_chars=8, extra_boundaries=EXTRA)
    assert em_clause == "I am not sure I caught that —"
    assert em_rest == " could you repeat it?"
    assert en_clause == "I am not sure I caught that –"
    assert en_rest == " could you repeat it?"


def test_colon_glued_to_digit_is_not_a_time_or_ratio_boundary():
    """A boundary character only counts if it ends the buffer or is followed
    by whitespace. A colon in a clock time or ratio is glued to the next
    digit -- not a clause boundary, even though ':' is in extra_boundaries.
    check_calendar reads times aloud, so this is a real, common case."""
    examples = [
        "Your meeting is at 3:30 this afternoon.",
        "The ratio is 16:9 on that display.",
    ]
    for text in examples:
        clause, rest = first_complete_clause(text, min_chars=12, extra_boundaries=EXTRA)
        assert clause == text, f"{text!r} split early: clause={clause!r}"
        assert rest == ""
        assert not re.search(r"\d:$", clause), f"{text!r} ended on a glued colon"


def test_period_glued_to_digit_is_not_a_decimal_boundary():
    """Same whitespace-adjacency rule applied to the default '.?!' set: a
    period inside a decimal number is glued to the next digit, so it must not
    be mistaken for a sentence end -- no extra_boundaries needed to see this,
    every existing '.' caller was exposed to it once first_chunk_min_chars
    dropped below the length of a typical decimal-bearing sentence."""
    examples = [
        "Your battery is at 82.5 percent.",
        "You have 2.5 hours of charge remaining.",
    ]
    for text in examples:
        clause, rest = first_complete_clause(text, min_chars=12)
        assert clause == text, f"{text!r} split early: clause={clause!r}"
        assert rest == ""
        assert not re.search(r"\d\.$", clause), f"{text!r} ended on a glued decimal point"


def test_boundary_followed_by_space_or_buffer_end_still_splits():
    """Positive case for every boundary type this rule touches: comma,
    semicolon, colon-followed-by-space, em-dash, en-dash, ellipsis (the third
    dot in "Well..." is followed by a space, so it still counts)."""
    cases = [
        ("Traffic is heavy; expect delays of 20 minutes.", "Traffic is heavy;", EXTRA),
        ("I checked your calendar: you have three events.", "I checked your calendar:", EXTRA),
        ("I'm doing well, thanks for asking.", "I'm doing well,", EXTRA),
        ("Let me check that — one moment.", "Let me check that —", EXTRA),
        ("Let me check that – one moment.", "Let me check that –", EXTRA),
        ("Well... let me think about that for a second.", "Well...", ""),
    ]
    for text, expected_clause, extra in cases:
        clause, _ = first_complete_clause(text, min_chars=1, extra_boundaries=extra)
        assert clause == expected_clause, f"{text!r}: got {clause!r}, expected {expected_clause!r}"


def test_abbreviation_period_is_not_a_sentence_boundary():
    """Lowering first_chunk_min_chars to 12 made these reachable (previously
    the min_chars=24 floor happened to cover most of them). A known
    abbreviation's period must not be mistaken for a sentence end -- these
    are real spoken phrases (navigation, calendar, asides), not edge cases."""
    examples = [
        "Take the exit near St. Mary and continue straight.",
        "You can use e.g. the voice command for that.",
        "Your appointment is with Dr. Smith at noon tomorrow.",
        "Please ask Dr. Smith about it.",
    ]
    for text in examples:
        clause, rest = first_complete_clause(text, min_chars=12)
        assert clause == text, f"{text!r} split early at an abbreviation: clause={clause!r}"
        assert rest == ""


def test_initial_period_is_not_a_sentence_boundary():
    text = "Please welcome J. R. Smith to the stage."
    clause, rest = first_complete_clause(text, min_chars=5)
    assert clause == text, f"split early at an initial: clause={clause!r}"
    assert rest == ""


def test_abbreviation_guard_does_not_block_a_real_sentence_end():
    """The guard must be specific to known abbreviations/initials -- it must
    not suppress a legitimate sentence end."""
    clause, rest = first_complete_clause(
        "I called the doctor. She was busy.", min_chars=5
    )
    assert clause == "I called the doctor."
    assert rest == " She was busy."


def test_trailing_decimal_at_buffer_end_waits_for_more_input():
    """A period at the very end of the buffer, preceded by a digit, is a
    number still being streamed in -- must not split yet."""
    clause, rest = first_complete_clause("The value is 3.", min_chars=5)
    assert clause is None
    assert rest == "The value is 3."


def test_validate_spoken_unit_accepts_comma_terminated_fragments():
    """The main risk: a comma fragment must not be rejected as malformed/empty,
    which would make Nova speak the fallback line instead of the real reply."""
    fragments = [
        "I'm doing well,",
        "Uh, I'm Nova,",
        "I can chat, look up the web,",
        "I'm not sure I caught that —",
        "Sure thing;",
        "Let me check:",
    ]
    for frag in fragments:
        result = validate_spoken_unit(frag)
        assert result["ok"] is True, f"{frag!r} was rejected: {result['failures']}"
        assert result["speak"], f"{frag!r} produced empty speak text"
        assert not result["fallback"], f"{frag!r} fell back instead of speaking the fragment"
