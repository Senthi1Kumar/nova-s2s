"""Optional MicroKWS loader stub — avoids hard TFLite dependency on CI.

Callers: nova_hailo.wake_kws.WakeWordDetector.
"""
from __future__ import annotations

from typing import Any


def try_load_micro_kws(model_path: str, threshold: float = 0.65) -> Any | None:
    _ = model_path
    try:
        from kws.micro_kws import MicroKWS  # type: ignore

        eng = MicroKWS(threshold=threshold, consecutive_triggers=2)
        if hasattr(eng, "load_model"):
            if hasattr(eng, "model_path"):
                eng.model_path = model_path
            if not eng.load_model():
                return None
        return eng
    except Exception:
        return None
