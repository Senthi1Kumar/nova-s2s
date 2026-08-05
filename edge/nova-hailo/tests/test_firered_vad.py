"""FireRedVAD segmenter tests.

Callers: pytest on laptop/Pi. Exercises nova_hailo.web.firered_vad.
Needs models/firered_vad/{*.onnx,cmvn.npz} + onnxruntime + kaldi_native_fbank.

Two tiers, mirroring test_silero_vad.py's skip-when-absent philosophy:
  - test_state_machine_*: stubs the ONNX model with a scripted probability
    sequence, so it exercises FireRedVadSegmenter's turn-taking logic (the
    part we wrote and could have bugs in) without depending on the model
    recognising synthetic audio as speech. A plain sine tone does NOT trigger
    FireRedVAD (unlike Silero, which reacts to any energy pattern) -- verified
    directly: real spectral content is required, so a stub is the honest way
    to unit-test the state machine in isolation.
  - test_real_corpus_*: opt-in smoke test against a real recorded utterance,
    only when NOVA_FIRERED_TEST_WAV points at one (e.g. on the Pi, against
    bench/corpus/fixtures/audio/*.wav). Skipped, not failed, when unset.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from nova_hailo.config import ROOT

ONNX = ROOT / "models" / "firered_vad" / "fireredvad_stream_vad_with_cache.onnx"
CMVN = ROOT / "models" / "firered_vad" / "cmvn.npz"
ASSETS_PRESENT = ONNX.exists() and CMVN.exists()


@pytest.mark.skipif(not ASSETS_PRESENT, reason="firered_vad model assets not present")
def test_state_machine_start_stop_with_scripted_probs(monkeypatch):
    """10ms-frame probability script -> speech_started then speech_stopped."""
    pytest.importorskip("onnxruntime")
    pytest.importorskip("kaldi_native_fbank")
    from nova_hailo.web.firered_vad import (
        FRAME_SHIFT_SAMPLE,
        FireRedVadConfig,
        FireRedVadSegmenter,
    )

    seg = FireRedVadSegmenter(
        FireRedVadConfig(
            speech_threshold=0.5, min_silence_ms=100, min_speech_ms=50, speech_pad_ms=50
        )
    )

    # low -> high (speech) -> low (silence), long enough to clear min_speech
    # and min_silence at 10ms/frame.
    script = [0.05] * 10 + [0.9] * 40 + [0.05] * 30
    it = iter(script)
    monkeypatch.setattr(seg, "_model", lambda _frame: next(it, 0.05))

    total_frames = len(script)
    pcm = np.zeros(total_frames * FRAME_SHIFT_SAMPLE, dtype=np.int16).tobytes()

    events = []
    step_bytes = FRAME_SHIFT_SAMPLE * 2  # int16 -> bytes, one frame per feed()
    for i in range(0, len(pcm), step_bytes):
        events.extend(seg.feed(pcm[i : i + step_bytes]))

    kinds = [e[0] for e in events]
    assert "speech_started" in kinds
    assert "speech_stopped" in kinds
    assert kinds.index("speech_started") < kinds.index("speech_stopped")

    stop_audio = next(a for k, a in events if k == "speech_stopped")
    assert stop_audio is not None and len(stop_audio) > 0


@pytest.mark.skipif(not ASSETS_PRESENT, reason="firered_vad model assets not present")
def test_reset_clears_state():
    pytest.importorskip("onnxruntime")
    pytest.importorskip("kaldi_native_fbank")
    from nova_hailo.web.firered_vad import FireRedVadSegmenter

    seg = FireRedVadSegmenter()
    seg.feed(np.zeros(1600, dtype=np.int16).tobytes())
    seg.reset()
    assert seg._triggered is False
    assert seg._sample_i == 0
    assert seg._pending_len == 0


@pytest.mark.skipif(
    not (ASSETS_PRESENT and os.environ.get("NOVA_FIRERED_TEST_WAV")),
    reason="set NOVA_FIRERED_TEST_WAV to a real speech wav to run this smoke test",
)
def test_real_corpus_detects_speech():
    pytest.importorskip("onnxruntime")
    pytest.importorskip("kaldi_native_fbank")
    import soundfile as sf

    from nova_hailo.web.firered_vad import FireRedVadSegmenter

    wav_path = Path(os.environ["NOVA_FIRERED_TEST_WAV"])
    wav_i16, sr = sf.read(str(wav_path), dtype="int16")
    assert sr == 16000

    seg = FireRedVadSegmenter()
    events = []
    step = 320  # 20ms PCM frames, matching realtime delivery
    for i in range(0, len(wav_i16), step):
        events.extend(seg.feed(wav_i16[i : i + step].tobytes()))

    kinds = [e[0] for e in events]
    assert "speech_started" in kinds
    assert "speech_stopped" in kinds
