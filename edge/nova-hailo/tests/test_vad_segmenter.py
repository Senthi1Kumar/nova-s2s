"""Offline tests for webrtcvad segmenter (no Hailo).

Callers: pytest on laptop/Pi. Exercises nova_hailo.web.vad.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("webrtcvad")

from nova_hailo.web.vad import VadConfig, WebRtcVadSegmenter  # noqa: E402


def _tone_pcm16(sec: float, sr: int = 16000, freq: float = 440.0, amp: float = 0.3) -> bytes:
    t = np.arange(int(sec * sr), dtype=np.float32) / sr
    x = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    return x.tobytes()


def _silence_pcm16(sec: float, sr: int = 16000) -> bytes:
    n = int(sec * sr)
    return np.zeros(n, dtype=np.int16).tobytes()


def test_vad_emits_speech_started_and_stopped():
    seg = WebRtcVadSegmenter(VadConfig(aggressiveness=1, end_silence_frames=10, min_utterance_s=0.3))
    events = []
    blob = _tone_pcm16(0.6) + _silence_pcm16(0.5)
    step = 960 * 2
    for i in range(0, len(blob), step):
        events.extend(seg.feed(blob[i : i + step]))
    kinds = [e[0] for e in events]
    assert "speech_started" in kinds
    assert "speech_stopped" in kinds
    stopped = [e[1] for e in events if e[0] == "speech_stopped"]
    assert stopped and len(stopped[0]) > 0


def test_threshold_maps_aggressiveness():
    # Callers: pytest. API: set_threshold maps UI 0..1 → mode + min_frame_rms.
    seg = WebRtcVadSegmenter()
    seg.set_threshold(0.2)
    assert seg.cfg.aggressiveness == 2
    assert seg.cfg.min_frame_rms > 0.01
    seg.set_threshold(0.9)
    assert seg.cfg.aggressiveness == 3
    assert seg.cfg.start_speech_frames >= 5
