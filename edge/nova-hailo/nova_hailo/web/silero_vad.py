"""Silero VAD v5 (ONNX + numpy) — same family as HF speech-to-speech.

Callers: nova_hailo/web/vad.py create_vad_segmenter(), realtime_session.
API: SileroVadSegmenter.feed(pcm16) → [(speech_started|speech_stopped, audio|None)]
Model file: models/silero_vad.onnx (binary weights, no date schema).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nova_hailo.config import ROOT

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None

WINDOW = 512  # 16 kHz Silero chunk
CONTEXT = 64
STATE_SHAPE = (2, 1, 128)

DEFAULT_ONNX = ROOT / "models" / "silero_vad.onnx"
ONNX_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/"
    "src/silero_vad/data/silero_vad.onnx"
)


@dataclass
class SileroVadConfig:
    sample_rate: int = 16000
    threshold: float = 0.55
    # 350ms endpointed mid-sentence on natural speech, yielding 1-2 word
    # fragments ("It!", "The") that Whisper-Base then mangles. Conversational
    # turn-taking needs a longer hangover — measured 2026-07-29.
    min_silence_ms: int = 600
    min_speech_ms: int = 400
    speech_pad_ms: int = 200
    max_utterance_s: float = 8.0
    onnx_path: str | None = None


class _SileroOnnx:
    """Pure-numpy port of silero OnnxWrapper (16 kHz only)."""

    def __init__(self, path: str):
        if ort is None:
            raise RuntimeError("onnxruntime not installed")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            path,
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self.reset_states()

    def reset_states(self):
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, 0), dtype=np.float32)

    def __call__(self, x: np.ndarray) -> float:
        """x: float32 shape (512,) or (1, 512) → speech probability."""
        x = np.asarray(x, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != WINDOW:
            raise ValueError(f"expected {WINDOW} samples, got {x.shape[1]}")
        if self._context.shape[1] == 0:
            self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        x_in = np.concatenate([self._context, x], axis=1)
        ort_inputs = {
            "input": x_in,
            "state": self._state,
            "sr": np.array(16000, dtype=np.int64),
        }
        out, state = self.session.run(None, ort_inputs)
        self._state = state
        self._context = x_in[:, -CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


def ensure_silero_onnx(path: Path | None = None) -> Path:
    """Return path to ONNX; download once if missing."""
    p = Path(path) if path else DEFAULT_ONNX
    if p.exists() and p.stat().st_size > 100_000:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request

    print(f"Downloading Silero VAD ONNX → {p}")
    urllib.request.urlretrieve(ONNX_URL, str(p))
    return p


class SileroVadSegmenter:
    """Same event API as WebRtcVadSegmenter: speech_started / speech_stopped."""

    def __init__(self, cfg: SileroVadConfig | None = None):
        self.cfg = cfg or SileroVadConfig()
        onnx = ensure_silero_onnx(
            Path(self.cfg.onnx_path) if self.cfg.onnx_path else None
        )
        self._model = _SileroOnnx(str(onnx))
        self._buf = np.zeros(0, dtype=np.float32)
        self._utterance: list[np.ndarray] = []
        self._pre: list[np.ndarray] = []
        self._pre_samples = 0
        self._triggered = False
        self._temp_end = 0
        self._sample_i = 0
        self._active_speech = 0
        self._pad = int(self.cfg.sample_rate * self.cfg.speech_pad_ms / 1000)
        self._min_sil = int(self.cfg.sample_rate * self.cfg.min_silence_ms / 1000)
        self._min_speech = int(self.cfg.sample_rate * self.cfg.min_speech_ms / 1000)
        self._max_utt = int(self.cfg.max_utterance_s * self.cfg.sample_rate)
        self._neg = max(0.01, self.cfg.threshold - 0.15)
        print(f"VAD: Silero ONNX ({onnx.name}) thr={self.cfg.threshold}")

    def set_threshold(self, threshold: float):
        """Map UI 0..1 noise-gate → Silero speech probability threshold."""
        t = max(0.0, min(1.0, float(threshold)))
        self.cfg.threshold = 0.38 + t * 0.38
        self._neg = max(0.01, self.cfg.threshold - 0.15)
        self.cfg.min_silence_ms = int(500 + (1.0 - t) * 250)
        self._min_sil = int(self.cfg.sample_rate * self.cfg.min_silence_ms / 1000)

    def reset(self):
        self._model.reset_states()
        self._buf = np.zeros(0, dtype=np.float32)
        self._utterance.clear()
        self._pre.clear()
        self._pre_samples = 0
        self._triggered = False
        self._temp_end = 0
        self._sample_i = 0
        self._active_speech = 0

    def _remember_pre(self, chunk: np.ndarray):
        self._pre.append(chunk.copy())
        self._pre_samples += len(chunk)
        while self._pre and self._pre_samples > self._pad:
            first = self._pre[0]
            if self._pre_samples - len(first) >= self._pad:
                self._pre.pop(0)
                self._pre_samples -= len(first)
            else:
                drop = self._pre_samples - self._pad
                self._pre[0] = first[drop:]
                self._pre_samples -= drop
                break

    def feed(self, pcm16: bytes) -> list[tuple[str, np.ndarray | None]]:
        events: list[tuple[str, np.ndarray | None]] = []
        if not pcm16:
            return events
        incoming = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, incoming])

        while len(self._buf) >= WINDOW:
            chunk = self._buf[:WINDOW]
            self._buf = self._buf[WINDOW:]
            self._sample_i += WINDOW
            prob = self._model(chunk)

            if prob >= self.cfg.threshold and self._temp_end:
                self._temp_end = 0

            if prob >= self.cfg.threshold and not self._triggered:
                self._triggered = True
                self._utterance = list(self._pre) + [chunk.copy()]
                self._pre.clear()
                self._pre_samples = 0
                self._active_speech = WINDOW
                events.append(("speech_started", None))
                continue

            if not self._triggered:
                self._remember_pre(chunk)
                continue

            self._utterance.append(chunk.copy())
            if prob >= self._neg:
                self._active_speech += WINDOW

            utt_len = sum(len(c) for c in self._utterance)
            if utt_len >= self._max_utt:
                audio = self._finish()
                if audio is not None:
                    events.append(("speech_stopped", audio))
                continue

            if prob < self._neg:
                if not self._temp_end:
                    self._temp_end = self._sample_i
                if self._sample_i - self._temp_end < self._min_sil:
                    continue
                audio = self._finish()
                if audio is not None:
                    events.append(("speech_stopped", audio))
                continue

        return events

    def _finish(self) -> np.ndarray | None:
        audio = (
            np.concatenate(self._utterance).astype(np.float32)
            if self._utterance
            else np.zeros(0, dtype=np.float32)
        )
        active = self._active_speech
        self._triggered = False
        self._temp_end = 0
        self._utterance.clear()
        self._active_speech = 0
        self._pre.clear()
        self._pre_samples = 0
        if active < self._min_speech or len(audio) < self._min_speech:
            return None
        return audio
