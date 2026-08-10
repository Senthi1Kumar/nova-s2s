"""Offline unit tests for Nemo post-commit wait budget math.

Callers: pytest on laptop/Pi. Pure function only — no Hailo, no sidecar, no WS.
"""
from __future__ import annotations

from nova_hailo.web.nemo_wait_budget import (
    DEFAULT_NEMO_WAIT_BASE_S,
    DEFAULT_NEMO_WAIT_CAP_S,
    DEFAULT_NEMO_WAIT_FLOOR_S,
    DEFAULT_NEMO_WAIT_SCALE,
    nemo_wait_budget_s,
)


def test_defaults_match_historical_formula():
    assert DEFAULT_NEMO_WAIT_FLOOR_S == 0.35
    assert DEFAULT_NEMO_WAIT_CAP_S == 1.8
    assert DEFAULT_NEMO_WAIT_BASE_S == 0.25
    assert DEFAULT_NEMO_WAIT_SCALE == 0.40


def test_short_utterance_hits_floor():
    # base + scale * 0 = 0.25 < floor 0.35 → floor
    assert nemo_wait_budget_s(0.0) == 0.35
    # base + scale * 0.2 = 0.33 < floor
    assert nemo_wait_budget_s(0.2) == 0.35
    # base + scale * 0.25 = 0.35 exactly at floor
    assert nemo_wait_budget_s(0.25) == 0.35


def test_mid_scales_linearly():
    # base + scale * 1.0 = 0.25 + 0.40 = 0.65
    assert abs(nemo_wait_budget_s(1.0) - 0.65) < 1e-9
    # base + scale * 2.0 = 0.25 + 0.80 = 1.05
    assert abs(nemo_wait_budget_s(2.0) - 1.05) < 1e-9


def test_long_utterance_hits_cap():
    # base + scale * 10 = 0.25 + 4.0 = 4.25 → cap 1.8
    assert nemo_wait_budget_s(10.0) == 1.8
    # just over where base+scale*sec = cap: (1.8 - 0.25) / 0.40 = 3.875
    assert nemo_wait_budget_s(3.875) == 1.8
    assert nemo_wait_budget_s(4.0) == 1.8


def test_negative_audio_clamped_to_zero():
    assert nemo_wait_budget_s(-1.0) == 0.35


def test_custom_knobs():
    assert nemo_wait_budget_s(1.0, floor=0.1, cap=2.0, base=0.0, scale=0.5) == 0.5
    assert nemo_wait_budget_s(0.0, floor=0.5, cap=2.0, base=0.0, scale=1.0) == 0.5
    assert nemo_wait_budget_s(10.0, floor=0.1, cap=0.9, base=0.0, scale=1.0) == 0.9


def test_sidecar_endpointing_forced_off_in_source():
    """Safety: endpointing must remain hard-coded false (not config-driven)."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "nova_hailo" / "backends" / "nemo_speech_sidecar.py"
    body = src.read_text()
    assert "--asr.endpointing.enable=false" in body
    # Must not appear as a configurable kwarg or fmt string of enable=true
    assert "endpointing.enable=true" not in body
