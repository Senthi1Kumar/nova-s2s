"""Whisper-Base ASR on the NPU (hailo_platform.genai.Speech2Text; does not use the KV slot)."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

import numpy as np

from ..services import AsrService
from .npu import NpuPool
from .registry import Registry

MAX_AUDIO_S = 60


class HailoWhisper(AsrService):
    MODEL_ID = "whisper-base"

    def __init__(self, pool: NpuPool, registry: Registry | None = None,
                 hef_path: str | None = None) -> None:
        self.pool, self.registry = pool, registry
        self.hef = hef_path or os.path.join(pool.settings.hef_dir, "Whisper-Base.hef")
        self._s2t: Any = None
        self._lock = threading.Lock()  # one transcription at a time

    def load(self) -> bool:
        def loader() -> Any:
            if not os.path.exists(self.hef):
                raise FileNotFoundError(f"HEF not found: {self.hef}")
            from hailo_platform.genai import Speech2Text

            return Speech2Text(self.pool.vdevice, self.hef)

        if self.registry is None:
            self._s2t = loader()
            return True
        self._s2t = self.registry.load(self.MODEL_ID, loader)
        return self._s2t is not None

    def _run(self, pcm: np.ndarray, language: str | None) -> str:
        with self._lock:
            kw = {"language": language} if language else {}
            return str(self._s2t.generate_all_text(pcm, **kw)).strip()

    async def transcribe(self, pcm16k: np.ndarray, language: str | None = None) -> str:
        await self.pool.wait_ready()
        if self._s2t is None:
            raise RuntimeError("whisper-base is not resident")
        pcm = np.array(pcm16k, dtype=np.float32, order="C").reshape(-1)  # writable copy
        if pcm.size == 0:
            return ""
        pcm = pcm[: MAX_AUDIO_S * 16000]
        t0 = time.perf_counter()
        text = await asyncio.to_thread(self._run, pcm, language)
        if self.registry is not None:
            self.registry.record_call(self.MODEL_ID, (time.perf_counter() - t0) * 1000.0)
        return text

    def close(self) -> None:
        if self._s2t is not None:
            try:
                self._s2t.release()
            except Exception:  # noqa: BLE001
                pass
            self._s2t = None
            if self.registry is not None:
                self.registry.set_state(self.MODEL_ID, "unloaded")
