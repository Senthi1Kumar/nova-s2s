"""Fail-closed speak path pure helpers (gate reject / empty STT).

Used by realtime_session for the fail-closed empty-STT path. Kept free of
FastAPI / pipeline / Hailo imports so host unit tests can import it.
"""
from __future__ import annotations

# Fixed vetted fallback when gate rejects or STT returns empty — never silent, never LLM.
FAIL_CLOSED_SPEAK = "I didn't catch that."
FAIL_CLOSED_COOLDOWN_S = 1.5


def should_speak_fail_closed(
    last_mono: float,
    now: float,
    cooldown: float = FAIL_CLOSED_COOLDOWN_S,
) -> bool:
    """Return True when the fail-closed line should be spoken (TTS).

    False while still inside the cooldown window after a previous speak.
    ``last_mono`` is typically ``float("-inf")`` before any fail-closed speak.
    """
    return (now - last_mono) >= cooldown
