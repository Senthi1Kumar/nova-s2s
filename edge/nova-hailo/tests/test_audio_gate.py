"""Offline tests for utterance energy gate.

Callers: pytest on laptop/Pi. Exercises nova_hailo.audio_gate.accept_utterance.
No data files; synthetic float32 PCM only.
"""
from __future__ import annotations

import numpy as np

from nova_hailo.audio_gate import accept_utterance


def test_rejects_quiet_noise():
    x = np.random.randn(16000).astype(np.float32) * 0.008
    ok, reason, st = accept_utterance(x)
    assert not ok
    assert "low_rms" in reason or "low_speech" in reason or "low_peak" in reason
    assert st["rms"] < 0.016


def test_accepts_loud_speech_like():
    t = np.arange(16000 * 1, dtype=np.float32) / 16000.0
    env = (np.sin(2 * np.pi * 3 * t) * 0.5 + 0.5).astype(np.float32)
    x = (np.sin(2 * np.pi * 180 * t) * env * 0.35).astype(np.float32)
    ok, reason, st = accept_utterance(x)
    assert ok, (reason, st)
