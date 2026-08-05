"""Optional wake-word (KWS) for OEM demo — PTT remains the fail-safe.

Callers: nova_hailo.web.realtime_session when wake.kws_enabled.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger("nova_hailo.kws")


class WakeWordDetector:
    """Returns no detections when model unavailable (fail-open to PTT)."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        threshold: float = 0.65,
        model_path: str | None = None,
    ):
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self._engine = None
        self.available = False
        if not self.enabled:
            return
        path = model_path or os.environ.get("NOVA_HAILO_KWS_MODEL") or ""
        if not path:
            candidates = [
                Path("models/kws/micro_kws.tflite"),
                Path.home() / "nsk" / "nova_ai" / "nova" / "backend" / "kws" / "models",
            ]
            for c in candidates:
                if c.is_file():
                    path = str(c)
                    break
                if c.is_dir():
                    tflites = list(c.glob("*.tflite"))
                    if tflites:
                        path = str(tflites[0])
                        break
        if not path or not Path(path).is_file():
            logger.warning("KWS enabled but no model found — PTT only")
            self.enabled = False
            return
        try:
            from nova_hailo.tools._micro_kws_stub import try_load_micro_kws

            self._engine = try_load_micro_kws(path, threshold=self.threshold)
            self.available = self._engine is not None
            if not self.available:
                self.enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("KWS load failed: %s — PTT only", exc)
            self.enabled = False
            self.available = False

    def process(self, pcm16: bytes | np.ndarray) -> bool:
        if not self.enabled or self._engine is None:
            return False
        try:
            if isinstance(pcm16, (bytes, bytearray)):
                audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.asarray(pcm16, dtype=np.float32)
            detected, _lat = self._engine.process_chunk(audio)
            return bool(detected)
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "threshold": self.threshold,
            "mode": "kws" if self.available else "ptt",
        }
