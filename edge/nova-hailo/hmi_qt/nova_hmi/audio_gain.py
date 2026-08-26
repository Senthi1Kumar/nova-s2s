"""Mic gain / device pick helpers (no Qt)."""
from __future__ import annotations

import math
import os

NOISE_FLOOR = 0.004
HOT_RMS = 0.03
WIDE_DEFAULT_CH = 8
# Pi talk-and-replay: AGC off on WM8960; 3–4× manual gain. Override via env.
MANUAL_GAIN = float(os.environ.get("NOVA_HMI_MIC_GAIN", "3.5"))
AGC_ENABLED = os.environ.get("NOVA_HMI_AGC", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """PCM16 resample. Integer ratios match the Pi talk-and-replay bench."""
    try:
        import numpy as np
    except ImportError:
        return pcm
    if not pcm or int(src_rate) == int(dst_rate) or src_rate <= 0 or dst_rate <= 0:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return pcm
    src_rate, dst_rate = int(src_rate), int(dst_rate)
    if src_rate % dst_rate == 0:
        k = src_rate // dst_rate
        n = x.size // k
        if n == 0:
            return b""
        y = x[: n * k].reshape(n, k).mean(axis=1)
    elif dst_rate % src_rate == 0:
        k = dst_rate // src_rate
        n = x.size
        t = np.arange(n * k, dtype=np.float32) / float(k)
        idx = np.minimum(t, n - 1)
        lo = np.floor(idx).astype(np.int32)
        hi = np.minimum(lo + 1, n - 1)
        frac = (idx - lo).astype(np.float32)
        y = x[lo] * (1.0 - frac) + x[hi] * frac
    else:
        n_out = max(1, int(round(x.size * dst_rate / float(src_rate))))
        t = np.linspace(0.0, 1.0, n_out, endpoint=False)
        idx = t * (x.size - 1)
        lo = np.floor(idx).astype(np.int32)
        hi = np.minimum(lo + 1, x.size - 1)
        frac = idx - lo
        y = x[lo] * (1.0 - frac) + x[hi] * frac
    return np.clip(np.round(y), -32768, 32767).astype(np.int16).tobytes()


def uplink_multiplier(*, rms: float, agc: float) -> float:
    """Fixed near-field gain. Software AGC stays off unless NOVA_HMI_AGC=1."""
    del rms
    if AGC_ENABLED:
        return float(agc)
    return float(max(0.1, min(8.0, MANUAL_GAIN)))


def level_from_rms(rms: float) -> float:
    """Map raw (pre-AGC) RMS to 0..1. Silence stays near 0; speech moves."""
    db = 20 * math.log10(max(float(rms), 1e-6))
    return max(0.0, min(1.0, (db + 50.0) / 40.0))


def should_send_uplink(*, rms: float, blocked: bool, muted: bool) -> bool:
    """Quiet ALC attack must still go to the server — Silero needs the onset."""
    del rms
    return not blocked and not muted


def pick_input_index(devices: list, default_channels: int) -> int | None:
    """Pin WM8960 only when the default input is a wide Pulse device."""
    if int(default_channels or 0) <= WIDE_DEFAULT_CH:
        return None
    best: tuple[int, int] | None = None
    for i, d in enumerate(devices):
        name = str(d.get("name") or "").lower()
        n_in = int(d.get("max_input_channels") or 0)
        if n_in <= 0:
            continue
        score = 0
        if "wm8960" in name:
            score += 80
        if name.strip() in {"capture", "array"}:
            score += 40
        if "hdmi" in name or "vc4" in name:
            score -= 80
        if best is None or score > best[0]:
            best = (score, i)
    if best is None or best[0] <= 0:
        return None
    return best[1]
