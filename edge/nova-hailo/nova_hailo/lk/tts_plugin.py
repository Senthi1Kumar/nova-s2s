"""PiperTTS behind LiveKit's TTS interface.

LiveKit does not consume an async iterator: ChunkedStream._run() is handed an
AudioEmitter and pushes into it. QueuedOnnxTTS synthesises on a worker thread,
so PcmBridge carries chunks across that boundary with backpressure -- a slow
consumer slows synthesis rather than silently losing audio.

The declared sample rate comes from the caller or, failing that, from the
engine's voice config (Piper voices differ: en_US-amy-low is 16000 Hz). There is
no default. Every chunk's rate is checked against the declared one and a
mismatch fails loudly -- declaring one rate and pushing another plays the voice
at the wrong pitch. No resampling is done.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np
from livekit.agents import tts as lk_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.utils import shortuuid

from nova_hailo.lk.bridge import PcmBridge, ProducerClosed

logger = logging.getLogger("nova.lk.tts")

# Upper bound on waiting for a stream's synthesis thread to leave the engine
# before the next stream may enter it. A Piper chunk is well under a second on a
# Pi, and an aborted stream has already been interrupted, so this is generous.
_PRODUCER_JOIN_TIMEOUT_S = 5.0


def _to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def _engine_sample_rate(engine) -> int | None:
    voice = getattr(engine, "_voice", None)
    config = getattr(voice, "config", None)
    rate = getattr(config, "sample_rate", None)
    return int(rate) if rate else None


class NovaTTS(lk_tts.TTS):
    def __init__(self, engine, *, sample_rate: int | None = None) -> None:
        # PiperTTS reports a failed model load as enabled=False with no voice.
        # Say so, instead of letting the missing voice surface as a misleading
        # sample-rate error below.
        if getattr(engine, "enabled", True) is False:
            raise RuntimeError(
                "TTS engine failed to load (engine.enabled is False); "
                "check the TTS model path and the engine's load log"
            )
        if sample_rate is None:
            sample_rate = _engine_sample_rate(engine)
        if sample_rate is None:
            raise ValueError(
                "cannot determine the TTS sample rate: the engine exposes no "
                "_voice.config.sample_rate; pass sample_rate= explicitly"
            )
        super().__init__(
            # streaming=False: Piper synthesises a whole utterance. LiveKit wraps
            # us in its own StreamAdapter when a session needs incremental input.
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._engine = engine
        # One engine, one interrupt flag, one non-reentrant voice: streams take
        # turns. Owned here (per NovaTTS) because the engine is per NovaTTS.
        self._lock = asyncio.Lock()

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> lk_tts.ChunkedStream:
        return _NovaChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    @property
    def engine(self):
        return self._engine

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock


class _NovaChunkedStream(lk_tts.ChunkedStream):
    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        # Serialise on the engine. Everything below, including the interrupt and
        # the wait for the synthesis thread, happens while the lock is held, so
        # a later stream can neither clear this stream's late interrupt nor run
        # the voice concurrently with this stream's thread.
        async with self._tts.lock:
            await self._run_exclusive(output_emitter)

    async def _run_exclusive(self, output_emitter: lk_tts.AudioEmitter) -> None:
        engine = self._tts.engine
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=16)

        # The engine's interrupt flag is sticky until cleared. A stale one from
        # any earlier path must not silence this turn.
        clear = getattr(engine, "clear_interruption", None)
        if callable(clear):
            clear()

        output_emitter.initialize(
            request_id=shortuuid("nova-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )

        def produce() -> None:
            try:
                declared = self._tts.sample_rate
                for audio, sr in engine.synthesize_stream(self._input_text):
                    if audio is None or len(audio) == 0:
                        continue
                    if sr != declared:
                        raise ValueError(
                            f"TTS chunk sample rate {sr} Hz differs from the "
                            f"declared {declared} Hz; refusing to play at the wrong pitch"
                        )
                    bridge.push(_to_pcm16(audio))
            except ProducerClosed:
                return  # consumer aborted; stop synthesising
            except BaseException as exc:  # noqa: BLE001 - forwarded, not swallowed
                bridge.fail(exc)
                return
            bridge.close()

        thread = threading.Thread(target=produce, name="lk-tts-synth", daemon=True)
        thread.start()
        completed = False
        try:
            async for chunk in bridge:
                output_emitter.push(chunk)
            completed = True
        finally:
            bridge.abort()
            if not completed:
                # Cancellation or error: reach the worker thread so barge-in
                # stops generation instead of only stopping playback. Never on
                # normal completion, where the sticky flag would silence the
                # next turn.
                interrupt = getattr(engine, "interrupt", None)
                if callable(interrupt):
                    interrupt()
            await self._wait_for_producer(thread)
        output_emitter.flush()

    async def _wait_for_producer(self, thread: threading.Thread) -> None:
        """Wait for the synthesis thread to leave the engine, off the event loop."""
        await asyncio.to_thread(thread.join, _PRODUCER_JOIN_TIMEOUT_S)
        if thread.is_alive():
            logger.warning(
                "tts stream %r: synthesis thread still running %.1fs after the stream "
                "ended; the next stream may overlap it in the engine",
                self._input_text[:40],
                _PRODUCER_JOIN_TIMEOUT_S,
            )
