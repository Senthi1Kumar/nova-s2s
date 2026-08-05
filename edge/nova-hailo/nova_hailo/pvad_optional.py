"""Optional FireRedChat pVAD ONNX — feature-flagged off by default.

Callers: nova_hailo.web.realtime_session barge-in when pvad.enabled.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger("nova_hailo.pvad")


class OptionalFireRedPVAD:
    """Speaker-gated barge-in. Fail-open to Silero when disabled/unavailable."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_dir: str | None = None,
        voiceprint_wav: str | None = None,
        voiceprint_enc: str | None = None,
        threshold: float = 0.55,
        required_frames: int = 2,
    ):
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.required_frames = max(1, int(required_frames))
        self._pvad = None
        self.available = False
        self._hit = 0
        self._buf = np.zeros(0, dtype=np.float32)
        if not self.enabled:
            return
        mdir = model_dir or os.environ.get("NOVA_HAILO_PVAD_DIR") or "models/FireRedChat-pvad"
        onnx = Path(mdir) / "pvad.onnx"
        if not onnx.is_file():
            alt = (
                Path.home()
                / "work"
                / "repos"
                / "nova_ai"
                / "nova"
                / "backend"
                / "models"
                / "FireRedChat-pvad"
                / "pvad.onnx"
            )
            if alt.is_file():
                mdir = str(alt.parent)
                onnx = alt
        if not onnx.is_file():
            logger.warning("pVAD enabled but pvad.onnx missing — Silero only")
            self.enabled = False
            return
        try:
            import importlib

            mod = None
            for name in ("nova_hailo.pvad_firered", "pipeline_mp.pvad_firered"):
                try:
                    mod = importlib.import_module(name)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if mod is None:
                logger.warning("FireRedPVAD module not importable — Silero only")
                self.enabled = False
                return
            cls = getattr(mod, "FireRedPVAD")
            kwargs: dict = {"model_dir": mdir}
            if voiceprint_enc:
                kwargs["voiceprint_enc"] = voiceprint_enc
            elif voiceprint_wav:
                kwargs["voiceprint_16k_wav"] = voiceprint_wav
            else:
                enc = os.environ.get("NOVA_HAILO_VOICEPRINT_ENC")
                wav = os.environ.get("NOVA_HAILO_VOICEPRINT_WAV")
                if enc:
                    kwargs["voiceprint_enc"] = enc
                elif wav:
                    kwargs["voiceprint_16k_wav"] = wav
                else:
                    logger.warning("pVAD needs enrolled voiceprint — Silero only")
                    self.enabled = False
                    return
            self._pvad = cls(**kwargs)
            self.available = True
            logger.info("FireRedChat pVAD loaded from %s", mdir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pVAD init failed: %s — Silero only", exc)
            self.enabled = False
            self.available = False

    def reset(self) -> None:
        self._hit = 0
        self._buf = np.zeros(0, dtype=np.float32)
        if self._pvad is not None:
            try:
                self._pvad.reset()
            except Exception:  # noqa: BLE001
                pass

    def feed_pcm16(self, pcm16: bytes) -> bool:
        if not self.enabled or self._pvad is None:
            return False
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, audio])
        triggered = False
        while self._buf.size >= 160:
            frame = self._buf[:160]
            self._buf = self._buf[160:]
            try:
                prob = float(self._pvad.is_target_speaking(frame))
            except Exception:  # noqa: BLE001
                self._hit = 0
                continue
            if prob >= self.threshold:
                self._hit += 1
                if self._hit >= self.required_frames:
                    triggered = True
                    self._hit = 0
            else:
                self._hit = 0
        return triggered

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "threshold": self.threshold,
            "required_frames": self.required_frames,
        }
