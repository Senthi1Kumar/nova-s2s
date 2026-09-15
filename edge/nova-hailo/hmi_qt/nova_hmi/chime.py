"""Decode the local wake-word chime (MP3) once at HMI startup.

Callers: nova_hmi.audio.AudioDuplex.play_chime.
"""
from __future__ import annotations

import logging
from pathlib import Path

from nova_hmi.audio_gain import resample_int16

try:
    import numpy as np
    import soundfile as sf
except ImportError:  # pragma: no cover
    np = None
    sf = None

logger = logging.getLogger("nova_hmi.chime")

_ASSET = Path(__file__).resolve().parent / "assets" / "wake.mp3"


def default_chime_path() -> Path:
    return _ASSET


def load_chime(path: str | Path | None = None, dst_rate: int = 48000) -> tuple[bytes, int]:
    """Return mono PCM16 and sample rate. Empty bytes if decode fails."""
    src = Path(path) if path else _ASSET
    if np is None or sf is None:
        logger.warning("wake chime disabled (soundfile/numpy missing)")
        return b"", int(dst_rate)
    if not src.is_file():
        logger.warning("wake chime missing: %s", src)
        return b"", int(dst_rate)
    try:
        data, rate = sf.read(str(src), dtype="int16", always_2d=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("wake chime decode failed (%s): %s", src, exc)
        return b"", int(dst_rate)
    if data.size == 0:
        return b"", int(dst_rate)
    if data.shape[1] > 1:
        mono = data.astype(np.float32).mean(axis=1)
        pcm = np.clip(mono, -32768, 32767).astype(np.int16).tobytes()
    else:
        pcm = data[:, 0].tobytes()
    src_rate = int(rate)
    if src_rate != int(dst_rate):
        pcm = resample_int16(pcm, src_rate, int(dst_rate))
        src_rate = int(dst_rate)
    return pcm, src_rate
