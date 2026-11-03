"""Piper TTS on the CPU (thread), returning 16-bit mono WAV bytes."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import wave
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

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


class PhononTts(TtsService):
    MODEL_ID = "phonon-31m-en"

    def __init__(self, registry: Registry | None = None, duration_delta: float = -0.25) -> None:
        self.registry = registry
        self.duration_delta = duration_delta
        self._voice: Any = None
        self._lock = threading.Lock()

    def load(self) -> bool:
        def loader() -> Any:
            import ptts

            conditions = {"duration_delta": self.duration_delta} if self.duration_delta else {}
            return ptts.TTS(lang="en", conditions=conditions)

        if self.registry is None:
            self._voice = loader()
            return True
        self._voice = self.registry.load(self.MODEL_ID, loader)
        return self._voice is not None

    def _run_stream(self, text: str):
        def to_pcm(samples):
            return np.rint(np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()

        with self._lock, self._voice.stream(text) as audio:
            rate = int(audio.sample_rate)
            window = rate // 50
            carry = np.empty(0, dtype=np.float32)
            quiet = []
            for chunk in audio:
                samples = np.asarray(chunk, dtype=np.float32).reshape(-1)
                if carry.size:
                    samples = np.concatenate((carry, samples))
                usable = samples.size // window * window
                carry = samples[usable:]
                output = []
                for offset in range(0, usable, window):
                    frame = samples[offset:offset + window]
                    if np.max(np.abs(frame)) > 0.01:
                        output.extend(quiet)
                        quiet.clear()
                        output.append(frame)
                    else:
                        quiet.append(frame)
                if carry.size and np.max(np.abs(carry)) > 0.01:
                    output.extend(quiet)
                    quiet.clear()
                    output.append(carry)
                    carry = np.empty(0, dtype=np.float32)
                if output:
                    yield to_pcm(np.concatenate(output)), rate
            if carry.size:
                quiet.append(carry)
            if quiet:
                tail = np.concatenate(quiet)
                yield to_pcm(tail[-(rate // 10):]), rate

    async def synthesize_stream(self, text: str, voice: str | None = None) -> AsyncIterator[tuple[bytes, int]]:
        if self._voice is None:
            raise RuntimeError("phonon-31m-en is not loaded")
        if not text.strip():
            raise ValueError("empty text")
        started = time.perf_counter()
        stream = self._run_stream(text)
        end = object()
        try:
            while True:
                item = await asyncio.to_thread(next, stream, end)
                if item is end:
                    break
                yield item
        finally:
            if self.registry is not None:
                self.registry.record_call(self.MODEL_ID, (time.perf_counter() - started) * 1000.0)

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        chunks = [(pcm, rate) async for pcm, rate in self.synthesize_stream(text, voice)]
        if not chunks:
            raise RuntimeError("phonon returned no audio")
        rate = chunks[0][1]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setsampwidth(2)
            w.setnchannels(1)
            w.setframerate(rate)
            for pcm, _ in chunks:
                w.writeframes(pcm)
        return buf.getvalue()
