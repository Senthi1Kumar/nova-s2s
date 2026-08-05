"""Energy / quality gates so noise doesn't become Whisper turns.

Callers: web realtime_session, STT precheck.
Schema: accept_utterance() → (ok: bool, reason: str, stats: dict).
No date fields; audio is float32 PCM in memory only.
"""
from __future__ import annotations

from typing import Any

import numpy as np

# Web / noisy rooms: reject below this after DC removal (pre-boost).
# Slightly looser than first pass — Silero owns turn boundaries; this is a
# last-line filter against hush / HVAC that slipped past VAD.
DEFAULT_MIN_RMS = 0.016
DEFAULT_MIN_PEAK = 0.045
DEFAULT_MIN_SEC = 0.40
DEFAULT_MAX_SEC = 8.0
DEFAULT_MIN_SPEECH_FRAC = 0.15
DEFAULT_SPEECH_FRAME_RMS = 0.014


def utterance_stats(audio: np.ndarray, sample_rate: int = 16000) -> dict[str, Any]:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(x) == 0:
        return {"sec": 0.0, "rms": 0.0, "peak": 0.0, "speech_frac": 0.0}
    x = x - float(np.mean(x))
    rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
    peak = float(np.max(np.abs(x)) + 1e-12)
    sec = len(x) / float(sample_rate)
    frame = max(1, int(0.03 * sample_rate))
    n = len(x) // frame
    if n <= 0:
        speech_frac = 0.0
    else:
        frames = x[: n * frame].reshape(n, frame)
        fr = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        speech_frac = float(np.mean(fr >= DEFAULT_SPEECH_FRAME_RMS))
    return {
        "sec": round(sec, 3),
        "rms": round(rms, 5),
        "peak": round(peak, 5),
        "speech_frac": round(speech_frac, 3),
    }


def accept_utterance(
    audio: np.ndarray,
    *,
    sample_rate: int = 16000,
    min_rms: float = DEFAULT_MIN_RMS,
    min_peak: float = DEFAULT_MIN_PEAK,
    min_sec: float = DEFAULT_MIN_SEC,
    max_sec: float = DEFAULT_MAX_SEC,
    min_speech_frac: float = DEFAULT_MIN_SPEECH_FRAC,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether audio is worth sending to Whisper."""
    st = utterance_stats(audio, sample_rate)
    if st["sec"] < min_sec:
        return False, f"too_short({st['sec']:.2f}s)", st
    if st["sec"] > max_sec and st["rms"] < min_rms * 1.5:
        return False, f"long_quiet({st['sec']:.1f}s,rms={st['rms']})", st
    if st["rms"] < min_rms:
        return False, f"low_rms({st['rms']})", st
    if st["peak"] < min_peak:
        return False, f"low_peak({st['peak']})", st
    if st["speech_frac"] < min_speech_frac:
        return False, f"low_speech_frac({st['speech_frac']})", st
    return True, "ok", st
