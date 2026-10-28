"""Piper TTS on the CPU (thread), returning 16-bit mono WAV bytes."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import wave
from typing import Any

from ..services import TtsService
from .registry import Registry


class PiperTts(TtsService):
    MODEL_ID = "piper-amy"

    def __init__(self, voice_path: str, registry: Registry | None = None) -> None:
        self.voice_path, self.registry = voice_path, registry
        self._voice: Any = None
        self._lock = threading.Lock()

    def load(self) -> bool:
        def loader() -> Any:
            if not os.path.exists(self.voice_path):
                raise FileNotFoundError(f"Piper voice not found: {self.voice_path}")
            from piper import PiperVoice

            return PiperVoice.load(self.voice_path)

        if self.registry is None:
            self._voice = loader()
            return True
        self._voice = self.registry.load(self.MODEL_ID, loader)
        return self._voice is not None

    def _run(self, text: str) -> bytes:
        buf = io.BytesIO()
        with self._lock, wave.open(buf, "wb") as w:
            w.setsampwidth(2)
            w.setnchannels(1)
            w.setframerate(int(self._voice.config.sample_rate))
            for chunk in self._voice.synthesize(text):
                w.writeframes(chunk.audio_int16_bytes)
        return buf.getvalue()

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        if self._voice is None:
            raise RuntimeError("piper-amy is not loaded")
        if not text.strip():
            raise ValueError("empty text")
        t0 = time.perf_counter()
        wav = await asyncio.to_thread(self._run, text)
        if self.registry is not None:
            self.registry.record_call(self.MODEL_ID, (time.perf_counter() - t0) * 1000.0)
        return wav
