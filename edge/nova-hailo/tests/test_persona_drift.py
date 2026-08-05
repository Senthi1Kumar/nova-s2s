"""Persona-drift guard: response_contract._PERSONA_DRIFT_RE.

Callers: pytest. Exercises nova_hailo.response_contract.validate_spoken_unit.
Regression cover: identity/capability-decline/smalltalk canned strings must
still pass the contract once persona-drift patterns are rejected.
"""
from __future__ import annotations

import pytest

from nova_hailo.context_history import ConversationHistory
from nova_hailo.response_contract import _FALLBACK, validate_spoken_unit
from nova_hailo.tools.oem_tools import (
    _SMALLTALK,
    _UNAVAILABLE_CAPS,
    CAPABILITY_SPEAK,
    IDENTITY_SPEAK,
)


@pytest.mark.parametrize(
    "text",
    [
        "I'm not Nova anymore; I am now the vehicle called Nova.",
        "As an AI language model, I cannot provide that information.",
        "I am now the vehicle, not an assistant.",
        "Sorry, I am not Nova.",
    ],
)
def test_persona_drift_strings_fail_contract(text: str):
    result = validate_spoken_unit(text)
    assert result["ok"] is False
    assert "persona_drift" in result["failures"]
    assert result["speak"] == _FALLBACK


def test_fallback_itself_passes_contract():
    """A fallback that fails its own contract would loop forever."""
    result = validate_spoken_unit(_FALLBACK)
    assert result["ok"] is True
    assert result["speak"] == _FALLBACK


def test_identity_speak_passes_contract():
    result = validate_spoken_unit(IDENTITY_SPEAK)
    assert result["ok"] is True, result["failures"]
    assert result["speak"] == IDENTITY_SPEAK


def test_capability_speak_passes_contract():
    result = validate_spoken_unit(CAPABILITY_SPEAK)
    assert result["ok"] is True, result["failures"]


@pytest.mark.parametrize("entry", _UNAVAILABLE_CAPS, ids=lambda e: e[2][:30])
def test_unavailable_cap_declines_pass_contract(entry):
    _, _, speak = entry
    result = validate_spoken_unit(speak)
    assert result["ok"] is True, (speak, result["failures"])
    assert result["speak"] == speak


@pytest.mark.parametrize("entry", _SMALLTALK, ids=lambda e: e[1][:30])
def test_smalltalk_speak_passes_contract(entry):
    _, speak = entry
    result = validate_spoken_unit(speak)
    assert result["ok"] is True, (speak, result["failures"])
    assert result["speak"] == speak


def test_fallback_never_seeds_history():
    """pipeline.py:412 stores contract["speak"] via history.add() on every
    turn, including hard-failure turns where speak == _FALLBACK. Without a
    matching _REFUSAL_MARKERS entry this seeds the next prompt and recreates
    a repetition/refusal lock -- the model echoes its own last reply back
    turn after turn."""
    history = ConversationHistory(max_turns=2)
    history.add("some garbled ASR", _FALLBACK)
    assert len(history) == 0
