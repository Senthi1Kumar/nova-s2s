"""Offline Silero ONNX VAD smoke.

Callers: pytest. Exercises nova_hailo.web.silero_vad.SileroVadSegmenter.
Needs models/silero_vad.onnx + onnxruntime. No date schemas.
"""
from __future__ import annotations

import numpy as np
import pytest

from nova_hailo.config import ROOT

ONNX = ROOT / "models" / "silero_vad.onnx"


def test_silero_pad_defaults_match_bench():
    from nova_hailo.web.silero_vad import SileroVadConfig

    cfg = SileroVadConfig()
    assert cfg.speech_pad_ms == 400
    assert cfg.min_speech_ms == 400
    assert cfg.min_silence_ms == 600


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


def _short_voiced_pcm(seconds: float = 0.50) -> bytes:
    """A voiced burst too short to clear min_speech_ms, e.g. "stop it".

    0.50 s of tone measures ~192 ms of *voiced* frames on this model -- under
    min_speech_ms (400) and over barge_in_min_speech_ms (150), so it separates
    the two gates. Shorter than ~0.45 s and Silero never triggers at all, which
    would make both halves of the assertion vacuously true.
    """
    sr = 16000
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    env = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)).astype(np.float32)
    speech = (np.sin(2 * np.pi * 140 * t) * env * 0.4).astype(np.float32)
    silence = np.zeros(int(0.6 * sr), dtype=np.float32)
    pcm = np.concatenate([speech, silence])
    return (np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()


def _run_segmenter(seg, i16: bytes, *, mark_barge_in: bool) -> list:
    events = []
    step = 1600 * 2
    for i in range(0, len(i16), step):
        new = seg.feed(i16[i : i + step])
        if mark_barge_in and any(e[0] == "speech_started" for e in new):
            seg.mark_barge_in()
        events.extend(new)
    return [e for e in events if e[0] == "speech_stopped" and e[1] is not None]


def _barge_cfg():
    from nova_hailo.web.silero_vad import SileroVadConfig

    return SileroVadConfig(
        threshold=0.4,
        min_silence_ms=200,
        min_speech_ms=400,
        barge_in_min_speech_ms=150,
    )


@pytest.mark.skipif(not ONNX.exists(), reason="silero_vad.onnx not present")
def test_barge_in_utterance_survives_the_short_speech_gate():
    """min_speech_ms is an intent heuristic; a barge-in has already proven intent.

    The gate exists so a 1-2 word fragment never reaches STT as a cold turn.
    But an utterance that interrupted playback carries evidence a cold one
    does not, and rejecting it strands the session: audio stops, nothing is
    transcribed, and the user is left talking to a machine that quit listening.
    """
    pytest.importorskip("onnxruntime")
    from nova_hailo.web.silero_vad import SileroVadSegmenter

    i16 = _short_voiced_pcm()
    assert not _run_segmenter(
        SileroVadSegmenter(_barge_cfg()), i16, mark_barge_in=False
    ), "a cold short utterance must still be dropped"
    assert _run_segmenter(
        SileroVadSegmenter(_barge_cfg()), i16, mark_barge_in=True
    ), "a short barge-in utterance must reach STT"


@pytest.mark.skipif(not ONNX.exists(), reason="silero_vad.onnx not present")
def test_barge_in_relaxation_is_one_shot():
    """The next utterance is a fresh turn and faces the normal bar again."""
    pytest.importorskip("onnxruntime")
    from nova_hailo.web.silero_vad import SileroVadSegmenter

    seg = SileroVadSegmenter(_barge_cfg())
    i16 = _short_voiced_pcm()
    assert _run_segmenter(seg, i16, mark_barge_in=True), "barge-in utterance kept"
    # Same audio, no mark: the relaxation must not have persisted.
    assert not _run_segmenter(seg, i16, mark_barge_in=False)


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
