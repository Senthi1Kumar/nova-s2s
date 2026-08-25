"""Mic gain / device pick helpers (no Qt)."""
from __future__ import annotations

import math

NOISE_FLOOR = 0.004
HOT_RMS = 0.03
WIDE_DEFAULT_CH = 8


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
