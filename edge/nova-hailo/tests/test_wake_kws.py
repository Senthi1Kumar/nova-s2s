"""Wake-word facade + openWakeWord frame buffering — no live model required."""
from __future__ import annotations

import time

import numpy as np

from nova_hailo.backends.openwakeword_kws import FRAME_SAMPLES, OpenWakeWordKWS
from nova_hailo.wake_kws import WakeWordDetector


def test_facade_fail_open_on_missing_model(tmp_path, monkeypatch):
    monkeypatch.setattr("nova_hailo.backends.openwakeword_kws.ROOT", tmp_path)
    det = WakeWordDetector(
        enabled=True,
        engine="openwakeword",
        model_path=str(tmp_path / "missing.onnx"),
        threshold=0.15,
    )
    assert det.available is False
    assert det.process(np.zeros(960, dtype=np.int16).tobytes()) is False
    st = det.status()
    assert st["mode"] == "ptt"
    assert st["engine"] == "none"


def test_sixty_ms_batches_make_eighty_ms_frames():
    seen: list[int] = []

    def predict(frame: np.ndarray) -> float:
        seen.append(int(frame.size))
        return 0.0

    kws = OpenWakeWordKWS(predict_fn=predict, threshold=0.99, cooldown_s=0.0)
    batch = np.zeros(960, dtype=np.int16).tobytes()  # 60 ms @ 16 kHz
    kws.feed_pcm16(batch)
    assert seen == []
    kws.feed_pcm16(batch)
    assert seen == [FRAME_SAMPLES]
    kws.feed_pcm16(batch)
    assert seen == [FRAME_SAMPLES, FRAME_SAMPLES]


def test_threshold_consecutive_hits_and_cooldown():
    scores = [0.2, 0.2, 0.2, 0.2]

    def predict(_frame: np.ndarray) -> float:
        return float(scores.pop(0)) if scores else 0.0

    kws = OpenWakeWordKWS(
        predict_fn=predict,
        threshold=0.15,
        consecutive_hits=2,
        cooldown_s=10.0,
    )
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    hit1, s1 = kws.feed_pcm16(frame)
    assert hit1 is False
    assert s1 >= 0.15
    hit2, s2 = kws.feed_pcm16(frame)
    assert hit2 is True
    assert s2 >= 0.15
    hit3, _ = kws.feed_pcm16(frame)
    assert hit3 is False  # cooldown


def test_facade_openwakeword_with_fake_predictor(monkeypatch):
    def predict(_frame: np.ndarray) -> float:
        return 0.9

    fake = OpenWakeWordKWS(predict_fn=predict, threshold=0.15, cooldown_s=0.0)

    def _ctor(**kwargs):
        return fake

    monkeypatch.setattr(
        "nova_hailo.backends.openwakeword_kws.OpenWakeWordKWS",
        _ctor,
    )
    det = WakeWordDetector(enabled=True, engine="openwakeword", threshold=0.15)
    assert det.available is True
    pcm = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    assert det.process(pcm) is True
    assert det.last_score >= 0.15
    st = det.status()
    assert st["engine"] == "openwakeword"
    assert st["mode"] == "kws"


def test_below_threshold_never_fires():
    kws = OpenWakeWordKWS(
        predict_fn=lambda _f: 0.05,
        threshold=0.15,
        consecutive_hits=1,
        cooldown_s=0.0,
    )
    pcm = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    hit, score = kws.feed_pcm16(pcm)
    assert hit is False
    assert score == 0.05
    time.sleep(0.01)
    hit2, _ = kws.feed_pcm16(pcm)
    assert hit2 is False


def test_barge_threshold_and_skip_cooldown():
    kws = OpenWakeWordKWS(
        predict_fn=lambda _f: 0.12,
        threshold=0.15,
        consecutive_hits=1,
        cooldown_s=10.0,
    )
    pcm = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    miss, score = kws.feed_pcm16(pcm)
    assert miss is False
    assert score == 0.12
    hit, _ = kws.feed_pcm16(pcm, threshold=0.10, ignore_cooldown=True)
    assert hit is True
    hit2, _ = kws.feed_pcm16(pcm, threshold=0.10, ignore_cooldown=True)
    assert hit2 is True
