"""ParakeetSTT behind LiveKit's STT interface.

Non-streaming by design for Milestone 1. The NeMo streaming path is special-cased
in six places outside the STT factory, so wrapping it is a separate project;
_recognize_impl over a whole buffer is the honest first step.

transcribe() is blocking and CPU-bound, so it runs via asyncio.to_thread rather
than stalling the agent's event loop.

ParakeetSTT takes 16 kHz mono float32. Room audio arrives at whatever rate the
track negotiated (commonly 48 kHz), so every frame is brought to 16 kHz mono
here, using LiveKit's own resampler, before the engine sees it.
"""
from __future__ import annotations

import asyncio

import numpy as np
from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.agents.types import NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import AudioBuffer

STT_SAMPLE_RATE = 16000


def _mono_int16(frame: rtc.AudioFrame) -> np.ndarray:
    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
    return pcm


def _to_float32_mono(buffer: AudioBuffer) -> np.ndarray:
    """Concatenate frames into 16 kHz mono float32, honouring each frame's rate."""
    frames = buffer if isinstance(buffer, list) else [buffer]
    parts: list[np.ndarray] = []
    resampler: rtc.AudioResampler | None = None
    resampler_rate = 0

    def _flush() -> None:
        nonlocal resampler
        if resampler is not None:
            parts.extend(_mono_int16(f) for f in resampler.flush())
            resampler = None

    for frame in frames:
        pcm = _mono_int16(frame)
        if pcm.size == 0:
            continue
        if frame.sample_rate == STT_SAMPLE_RATE:
            _flush()
            parts.append(pcm)
            continue
        if resampler is None or resampler_rate != frame.sample_rate:
            _flush()
            resampler = rtc.AudioResampler(frame.sample_rate, STT_SAMPLE_RATE, num_channels=1)
            resampler_rate = frame.sample_rate
        parts.extend(_mono_int16(f) for f in resampler.push(bytearray(pcm.tobytes())))
    _flush()

    parts = [p for p in parts if p.size]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


class NovaSTT(lk_stt.STT):
    def __init__(self, engine) -> None:
        super().__init__(
            capabilities=lk_stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._engine = engine

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> lk_stt.SpeechEvent:
        audio = _to_float32_mono(buffer)
        text = ""
        if audio.size:
            text = await asyncio.to_thread(self._engine.transcribe, audio) or ""
        lang = language if isinstance(language, str) else "en"
        return lk_stt.SpeechEvent(
            type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[lk_stt.SpeechData(language=lang, text=text)],
        )
