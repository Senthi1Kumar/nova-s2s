"""Offline tests for Phase A never-silent fail-closed speak path.

Callers: pytest. Exercises pure helpers/constants in nova_hailo.web.fail_closed
and (lightly) router clarify speak — no live WS, no Hailo, no pipeline.
"""
from __future__ import annotations

from nova_hailo.edge_harness.router import CLARIFY_SPEAK
from nova_hailo.web.fail_closed import (
    FAIL_CLOSED_COOLDOWN_S,
    FAIL_CLOSED_SPEAK,
    should_speak_fail_closed,
)


def test_fail_closed_speak_constant():
    assert FAIL_CLOSED_SPEAK == "I didn't catch that."


def test_fail_closed_cooldown_constant():
    assert FAIL_CLOSED_COOLDOWN_S == 1.5


def test_fail_closed_speak_differs_from_router_clarify():
    """Gate/empty-STT fallback vs short-utterance clarify are distinct UX lines."""
    assert FAIL_CLOSED_SPEAK != CLARIFY_SPEAK
    assert CLARIFY_SPEAK == "Could you say that again?"


def test_should_speak_when_never_spoken():
    """Initial last_mono is -inf → always speak."""
    now = 1000.0
    assert should_speak_fail_closed(float("-inf"), now) is True
    assert should_speak_fail_closed(float("-inf"), now, FAIL_CLOSED_COOLDOWN_S) is True


def test_should_not_speak_within_cooldown():
    last = 100.0
    assert should_speak_fail_closed(last, last + 0.0) is False
    assert should_speak_fail_closed(last, last + 0.5) is False
    assert should_speak_fail_closed(last, last + 1.499) is False


def test_should_speak_at_or_after_cooldown():
    last = 100.0
    # Boundary: (now - last) >= cooldown → speak
    assert should_speak_fail_closed(last, last + FAIL_CLOSED_COOLDOWN_S) is True
    assert should_speak_fail_closed(last, last + 1.5) is True
    assert should_speak_fail_closed(last, last + 2.0) is True
    assert should_speak_fail_closed(last, last + 10.0) is True


def test_should_speak_custom_cooldown():
    last = 0.0
    assert should_speak_fail_closed(last, 0.9, cooldown=1.0) is False
    assert should_speak_fail_closed(last, 1.0, cooldown=1.0) is True
    assert should_speak_fail_closed(last, 1.1, cooldown=1.0) is True


def test_cooldown_zero_always_speaks_after_any_elapsed():
    """cooldown=0: only same-instant (delta < 0 never; delta >= 0) allows speak."""
    last = 5.0
    assert should_speak_fail_closed(last, last, cooldown=0.0) is True
    assert should_speak_fail_closed(last, last + 0.001, cooldown=0.0) is True
