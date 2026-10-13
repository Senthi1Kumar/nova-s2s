"""Optional wake-word (KWS) for OEM demo — PTT remains the fail-safe.

Callers: nova_hailo.web.realtime_session (LAN) and nova_hailo.lk.door (online),
both via WakeWordDetector.from_config so the two doors share one wake.* reading.
Engines: openwakeword (default) or legacy micro_kws.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger("nova_hailo.kws")


class WakeWordDetector:
    """Returns no detections when model unavailable (fail-open to PTT)."""

    @classmethod
    def from_config(cls, cfg) -> WakeWordDetector:
        return cls(
            enabled=bool(cfg.get("wake", "kws_enabled", default=False)),
            engine=str(cfg.get("wake", "kws_engine", default="openwakeword") or "openwakeword"),
            threshold=float(cfg.get("wake", "kws_threshold", default=0.15) or 0.15),
            model_path=cfg.get("wake", "kws_model"),
            consecutive_hits=int(cfg.get("wake", "kws_consecutive_hits", default=1) or 1),
            cooldown_s=float(cfg.get("wake", "kws_cooldown_s", default=1.5) or 1.5),
            barge_threshold=float(cfg.get("wake", "kws_barge_threshold", default=0.10) or 0.10),
        )

    def __init__(
        self,
        *,
        enabled: bool = False,
        threshold: float = 0.65,
        model_path: str | None = None,
        engine: str = "openwakeword",
        consecutive_hits: int = 1,
        cooldown_s: float = 1.5,
        barge_threshold: float | None = None,
    ):
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.barge_threshold = float(
            barge_threshold if barge_threshold is not None else min(self.threshold, 0.10)
        )
        self.engine_name = (engine or "openwakeword").strip().lower()
        self.consecutive_hits = max(1, int(consecutive_hits))
        self.cooldown_s = float(cooldown_s)
        self._engine = None
        self.available = False
        self.last_score = 0.0
        self.model_path = model_path or os.environ.get("NOVA_HAILO_KWS_MODEL") or ""
        if not self.enabled:
            return
        if self.engine_name in {"openwakeword", "oww", "onnx"}:
            self._load_openwakeword()
        else:
            self._load_micro_kws()

    def _load_openwakeword(self) -> None:
        try:
            from nova_hailo.backends.openwakeword_kws import OpenWakeWordKWS

            self._engine = OpenWakeWordKWS(
                model_path=self.model_path or None,
                threshold=self.threshold,
                consecutive_hits=self.consecutive_hits,
                cooldown_s=self.cooldown_s,
            )
            self.model_path = self._engine.model_path
            self.available = True
            self.engine_name = "openwakeword"
        except Exception as exc:  # noqa: BLE001
            logger.warning("openWakeWord load failed: %s — PTT only", exc)
            self.enabled = False
            self.available = False
            self._engine = None

    def _load_micro_kws(self) -> None:
        path = self.model_path
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
            self.model_path = path
            self.available = self._engine is not None
            if not self.available:
                self.enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("KWS load failed: %s — PTT only", exc)
            self.enabled = False
            self.available = False

    def process(
        self,
        pcm16: bytes | np.ndarray,
        *,
        threshold: float | None = None,
        ignore_cooldown: bool = False,
    ) -> bool:
        if not self.enabled or self._engine is None:
            return False
        try:
            if hasattr(self._engine, "feed_pcm16"):
                detected, score = self._engine.feed_pcm16(
                    pcm16, threshold=threshold, ignore_cooldown=ignore_cooldown
                )
                self.last_score = float(score)
                return bool(detected)
            if isinstance(pcm16, (bytes, bytearray)):
                audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.asarray(pcm16, dtype=np.float32)
            detected, _lat = self._engine.process_chunk(audio)
            return bool(detected)
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> dict:
        mode = "kws" if self.available else "ptt"
        return {
            "enabled": self.enabled,
            "available": self.available,
            "threshold": self.threshold,
            "barge_threshold": self.barge_threshold,
            "mode": mode,
            "engine": self.engine_name if self.available else "none",
            "model": self.model_path,
            "consecutive_hits": self.consecutive_hits,
            "cooldown_s": self.cooldown_s,
            "last_score": self.last_score,
        }
