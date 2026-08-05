"""Server-side VAD segmenters for OpenAI-realtime style turns.

Callers: nova_hailo/web/realtime_session.py via create_vad_segmenter().
Default: Silero ONNX (HF s2s family). Fallback: webrtcvad + energy gate.
Env: NOVA_HAILO_VAD=silero|webrtc (default silero).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

try:
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None


class VadSegmenter(Protocol):
    def set_threshold(self, threshold: float) -> None: ...
    def reset(self) -> None: ...
    def feed(self, pcm16: bytes) -> list[tuple[str, np.ndarray | None]]: ...


def create_vad_segmenter() -> Any:
    """Prefer Silero (HF s2s); fall back to webrtcvad. NOVA_HAILO_VAD=firered
    selects FireRedVAD as an A/B alternative (see nova_hailo/web/firered_vad.py)."""
    prefer = (os.environ.get("NOVA_HAILO_VAD") or "silero").strip().lower()
    if prefer in {"webrtc", "webrtcvad", "web"}:
        print("VAD: webrtcvad (forced)")
        return WebRtcVadSegmenter()
    if prefer in {"firered", "firered_vad", "firered-vad"}:
        try:
            from nova_hailo.web.firered_vad import FireRedVadSegmenter

            return FireRedVadSegmenter()
        except Exception as e:
            print(f"VAD: FireRedVAD unavailable ({e}); falling back to webrtcvad")
            return WebRtcVadSegmenter()
    try:
        from nova_hailo.web.silero_vad import SileroVadSegmenter

        return SileroVadSegmenter()
    except Exception as e:
        print(f"VAD: Silero unavailable ({e}); falling back to webrtcvad")
        return WebRtcVadSegmenter()


@dataclass
class VadConfig:
    sample_rate: int = 16000
    frame_ms: int = 30
    aggressiveness: int = 3
    start_speech_frames: int = 5  # ~150ms of voiced frames
    end_silence_frames: int = 18  # ~540ms hangover
    max_utterance_s: float = 8.0
    min_utterance_s: float = 0.55
    # Frame must clear this RMS (float) in addition to webrtcvad
    min_frame_rms: float = 0.016


class WebRtcVadSegmenter:
    """Feed PCM16 mono; emit speech_started / speech_stopped + float32 audio."""

    def __init__(self, cfg: VadConfig | None = None):
        if webrtcvad is None:
            raise RuntimeError("webrtcvad not installed")
        self.cfg = cfg or VadConfig()
        self._vad = webrtcvad.Vad(max(0, min(3, int(self.cfg.aggressiveness))))
        self._frame_bytes = int(self.cfg.sample_rate * self.cfg.frame_ms / 1000) * 2
        self._buf = bytearray()
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._utterance = bytearray()
        self._utterance_frames = 0

    def set_threshold(self, threshold: float):
        """Map 0..1 noise-gate threshold → aggressiveness + energy + hangover."""
        t = max(0.0, min(1.0, float(threshold)))
        # Default UI ~0.45–0.7 → mode 3; only loose mode when slider near Off
        self.cfg.aggressiveness = 2 if t < 0.25 else 3
        self._vad.set_mode(self.cfg.aggressiveness)
        self.cfg.end_silence_frames = int(14 + (1.0 - t) * 12)
        self.cfg.start_speech_frames = 4 if t < 0.4 else 5 if t < 0.75 else 6
        # Higher threshold → higher energy floor (fewer false starts)
        self.cfg.min_frame_rms = 0.010 + t * 0.028

    def reset(self):
        self._buf.clear()
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._utterance.clear()
        self._utterance_frames = 0

    def _frame_is_speech(self, frame: bytes) -> bool:
        try:
            vad_ok = self._vad.is_speech(frame, self.cfg.sample_rate)
        except Exception:
            vad_ok = False
        if not vad_ok:
            return False
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)) + 1e-12)
        return rms >= self.cfg.min_frame_rms

    def feed(self, pcm16: bytes) -> list[tuple[str, np.ndarray | None]]:
        events: list[tuple[str, np.ndarray | None]] = []
        self._buf.extend(pcm16)
        while len(self._buf) >= self._frame_bytes:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            is_speech = self._frame_is_speech(frame)

            if is_speech:
                self._speech_frames += 1
                self._silence_frames = 0
            else:
                self._silence_frames += 1
                self._speech_frames = 0

            if not self._in_speech:
                if self._speech_frames >= self.cfg.start_speech_frames:
                    self._in_speech = True
                    self._utterance = bytearray(frame)
                    self._utterance_frames = 1
                    events.append(("speech_started", None))
                continue

            self._utterance.extend(frame)
            self._utterance_frames += 1
            max_frames = int(self.cfg.max_utterance_s * 1000 / self.cfg.frame_ms)
            end = (
                self._silence_frames >= self.cfg.end_silence_frames
                or self._utterance_frames >= max_frames
            )
            if end:
                audio = self._bytes_to_float32(bytes(self._utterance))
                min_samples = int(self.cfg.min_utterance_s * self.cfg.sample_rate)
                self._in_speech = False
                self._silence_frames = 0
                self._speech_frames = 0
                self._utterance.clear()
                self._utterance_frames = 0
                if len(audio) >= min_samples:
                    events.append(("speech_stopped", audio))
        return events

    @staticmethod
    def _bytes_to_float32(pcm16: bytes) -> np.ndarray:
        if not pcm16:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
