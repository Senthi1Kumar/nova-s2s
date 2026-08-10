"""Pure Nemo streaming post-commit wait budget (no Hailo / no network).

Callers: realtime_session after speech_stopped when using the sidecar path;
unit tests for floor/scale/cap boundaries.
"""
from __future__ import annotations

# Defaults match the historical hardcoded formula in realtime_session:
#   min(1.8, max(0.35, 0.25 + 0.40 * audio_sec))
DEFAULT_NEMO_WAIT_FLOOR_S = 0.35
DEFAULT_NEMO_WAIT_CAP_S = 1.8
DEFAULT_NEMO_WAIT_BASE_S = 0.25
DEFAULT_NEMO_WAIT_SCALE = 0.40


def nemo_wait_budget_s(
    audio_sec: float,
    *,
    floor: float = DEFAULT_NEMO_WAIT_FLOOR_S,
    cap: float = DEFAULT_NEMO_WAIT_CAP_S,
    base: float = DEFAULT_NEMO_WAIT_BASE_S,
    scale: float = DEFAULT_NEMO_WAIT_SCALE,
) -> float:
    """Post-speech wait for streaming finalization, scaled to utterance length.

    Decode runs during capture; after commit only the RNNT right-context flush
    remains. Budget scales with utterance length for the offline-fallback path
    and is hard-capped so a dead socket cannot burn multi-second waits every turn.
    """
    sec = max(0.0, float(audio_sec))
    return min(float(cap), max(float(floor), float(base) + float(scale) * sec))
