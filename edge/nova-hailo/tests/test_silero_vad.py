"""Offline Silero ONNX VAD smoke.

Callers: pytest. Exercises nova_hailo.web.silero_vad.SileroVadSegmenter.
Needs models/silero_vad.onnx + onnxruntime. No date schemas.
"""
from __future__ import annotations

import numpy as np
import pytest

from nova_hailo.config import ROOT

ONNX = ROOT / "models" / "silero_vad.onnx"


@pytest.mark.skipif(not ONNX.exists(), reason="silero_vad.onnx not present")
def test_silero_detects_tone_as_speech_then_silence():
    pytest.importorskip("onnxruntime")
    from nova_hailo.web.silero_vad import SileroVadConfig, SileroVadSegmenter

    seg = SileroVadSegmenter(
        SileroVadConfig(threshold=0.4, min_silence_ms=200, min_speech_ms=250)
    )
    sr = 16000
    t = np.arange(int(0.8 * sr), dtype=np.float32) / sr
    env = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)).astype(np.float32)
    speech = (np.sin(2 * np.pi * 140 * t) * env * 0.4).astype(np.float32)
    silence = np.zeros(int(0.6 * sr), dtype=np.float32)
    pcm = np.concatenate([speech, silence])
    i16 = (np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()

    events = []
    step = 1600 * 2
    for i in range(0, len(i16), step):
        events.extend(seg.feed(i16[i : i + step]))

    kinds = [e[0] for e in events]
    assert "speech_started" in kinds
    assert "speech_stopped" in kinds
    stopped = [e[1] for e in events if e[0] == "speech_stopped"]
    assert stopped and stopped[0] is not None and len(stopped[0]) > 0


@pytest.mark.skipif(not ONNX.exists(), reason="silero_vad.onnx not present")
def test_silero_rejects_quiet_noise():
    pytest.importorskip("onnxruntime")
    from nova_hailo.web.silero_vad import SileroVadConfig, SileroVadSegmenter

    seg = SileroVadSegmenter(SileroVadConfig(threshold=0.55, min_speech_ms=400))
    noise = np.random.randn(16000).astype(np.float32) * 0.01
    i16 = (np.clip(noise, -1, 1) * 32767).astype(np.int16).tobytes()
    events = []
    step = 1600 * 2
    for i in range(0, len(i16), step):
        events.extend(seg.feed(i16[i : i + step]))
    stopped = [e for e in events if e[0] == "speech_stopped"]
    assert not stopped
