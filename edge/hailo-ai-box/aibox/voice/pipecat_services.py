"""Thin Pipecat frame processors over the existing AI Box ASR/chat/TTS services."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress

import numpy as np
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..services import Services
from .audio import CHUNK_BYTES, SAMPLE_RATE, pad_to_chunk, to_16k, wav_to_pcm16
from .text import SentenceBuffer
from .turn_controller import TurnAction, TurnController

log = logging.getLogger("aibox.voice")

MAX_PCM_BYTES = 60 * 16000 * 2
PARTIAL_INTERVAL_S = 0.5
PARTIAL_VAD_INTERVAL_S = 0.8
PARTIAL_MIN_SPEECH_GROWTH_MS = 300
PARTIAL_TAIL_BYTES = 10 * SAMPLE_RATE * 2
VAD_WINDOW_SAMPLES = 512
VAD_WINDOW_BYTES = VAD_WINDOW_SAMPLES * 2
VAD_WINDOW_MS = VAD_WINDOW_SAMPLES * 1000 // SAMPLE_RATE
VAD_INLINE_WINDOWS = 4
SPEECH_PAD_BYTES = 300 * SAMPLE_RATE * 2 // 1000
BARGE_IN_MIN_SPEECH_MS = 250
BARGE_IN_MIN_AUDIO_MS = 400
HALLUCINATION_MAX_SPEECH_MS = 600
HALLUCINATION_FILLERS = frozenset({"", "you", "thank you", "thanks", "bye"})
SYSTEM_PROMPT = "You are Nova, a voice assistant. Reply in English, in one or two short spoken sentences. Use plain spoken text only, without emojis, Markdown, or stage directions."
CAP_VOICE = 256


def _event(kind: str, **fields) -> OutputTransportMessageUrgentFrame:
    return OutputTransportMessageUrgentFrame(message={"type": kind, **fields})


def _ordered_event(kind: str, **fields) -> OutputTransportMessageFrame:
    return OutputTransportMessageFrame(message={"type": kind, **fields})


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def _create_vad():
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    vad = SileroVADAnalyzer(sample_rate=SAMPLE_RATE)
    vad.set_sample_rate(SAMPLE_RATE)
    return vad


class VoiceTurnProcessor(FrameProcessor):
    def __init__(
        self,
        services: Services,
        controller: TurnController | None = None,
        vad_factory: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        self.services = services
        self.controller = controller
        self._vad_factory = vad_factory or _create_vad
        self._vad = None
        self._vad_failed = False
        self._vad_pending = bytearray()
        self.total_windows = 0
        self.speech_windows = 0
        self.first_speech = -1
        self.last_speech = -1
        self.partial_speech_windows = 0
        self.model = "qwen3-vl"
        self.audio = bytearray()
        self.turn_id = 0
        self.active = False
        self.partial_task: asyncio.Task | None = None
        self.stop_task: asyncio.Task | None = None
        self.partial_revision = 0
        self.last_partial = 0.0
        self.latest_text = ""
        self.stop_started = 0.0
        self.barge_pending = False
        self.barge_ignored = False
        self.barge_model = ""
        self.barge_processing: dict = {}

    async def _cancel_partial(self) -> None:
        if self.partial_task is not None:
            self.partial_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.partial_task
            self.partial_task = None

    async def _cancel_stop(self) -> None:
        if self.stop_task is not None:
            self.stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stop_task
            self.stop_task = None

    async def _partial(self, turn_id: int, pcm: bytes) -> None:
        try:
            text = await self.services.asr.transcribe(
                np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0, None
            )
            if turn_id == self.turn_id and self.active and text.strip():
                self.latest_text = text.strip()
                self.partial_revision += 1
                await self.push_frame(_event(
                    "transcript_partial", turn_id=turn_id,
                    revision=self.partial_revision, text=self.latest_text,
                ))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - partial ASR is best-effort
            return
        finally:
            self.partial_task = None

    def _vad_enabled(self) -> bool:
        return self.services.settings.voice_vad and not self._vad_failed

    def _vad_state(self) -> str:
        if not self.services.settings.voice_vad:
            return "off"
        return "failed" if self._vad_failed else "on"

    def _reset_vad(self) -> None:
        self._vad_pending.clear()
        self.total_windows = self.speech_windows = self.partial_speech_windows = 0
        self.first_speech = self.last_speech = -1
        model = getattr(self._vad, "_model", None)
        if hasattr(model, "reset_states"):
            model.reset_states()

    def _score(self, data: bytes) -> None:
        threshold = self.services.settings.voice_vad_threshold
        for off in range(0, len(data), VAD_WINDOW_BYTES):
            confidence = self._vad.voice_confidence(data[off:off + VAD_WINDOW_BYTES])
            if isinstance(confidence, int):
                # pipecat logs scoring errors and returns the int 0 instead of raising
                raise TypeError("Silero VAD failed to score a window")
            if float(np.max(confidence)) >= threshold:
                self.speech_windows += 1
                if self.first_speech < 0:
                    self.first_speech = self.total_windows
                self.last_speech = self.total_windows
            self.total_windows += 1

    async def _feed_vad(self, pcm: bytes) -> None:
        try:
            if self._vad is None:
                self._vad = self._vad_factory()
            self._vad_pending.extend(pcm)
            usable = len(self._vad_pending) // VAD_WINDOW_BYTES * VAD_WINDOW_BYTES
            if not usable:
                return
            data = bytes(self._vad_pending[:usable])
            del self._vad_pending[:usable]
            if usable // VAD_WINDOW_BYTES > VAD_INLINE_WINDOWS:
                await asyncio.to_thread(self._score, data)
            else:
                self._score(data)
        except Exception as exc:  # noqa: BLE001 - a broken VAD must not swallow speech
            log.warning("voice VAD unavailable, falling back to ungated ASR: %s", exc)
            self._vad_failed = True

    def _should_start_partial(self, now: float) -> bool:
        settings = self.services.settings
        if not settings.voice_partials or self.partial_task is not None:
            return False
        if not self._vad_enabled():
            return now - self.last_partial >= PARTIAL_INTERVAL_S
        grown_ms = (self.speech_windows - self.partial_speech_windows) * VAD_WINDOW_MS
        return (
            grown_ms >= PARTIAL_MIN_SPEECH_GROWTH_MS
            and now - self.last_partial >= PARTIAL_VAD_INTERVAL_S
        )

    async def _ignore(self, turn_id: int, reason: str, speech_ms: int, ratio: float) -> None:
        log.info("voice ignored turn=%d reason=%s speech_ms=%d ratio=%.2f",
                 turn_id, reason, speech_ms, ratio)
        await self.push_frame(_event(
            "ignored", turn_id=turn_id, reason=reason, speech_ms=speech_ms, ratio=round(ratio, 3),
            vad=self._vad_state(),
        ))

    async def _finalize(self) -> None:
        if not self.active:
            await self.push_frame(_event("error", error={"code": "empty_audio", "message": "no active turn"}))
            return
        self.active = False
        turn_id = self.turn_id
        await self._cancel_partial()
        audio = bytes(self.audio[: len(self.audio) // 2 * 2])
        self.audio.clear()
        if not audio:
            await self.push_frame(_event("error", error={"code": "empty_audio", "message": "no audio received"}))
            return
        settings = self.services.settings
        speech_ms = self.speech_windows * VAD_WINDOW_MS
        ratio = self.speech_windows / max(self.total_windows, 1)
        if self._vad_enabled():
            if self.speech_windows == 0:
                return await self._ignore(turn_id, "no_speech", speech_ms, ratio)
            if speech_ms < settings.voice_min_speech_ms or ratio < settings.voice_min_speech_ratio:
                return await self._ignore(turn_id, "too_short", speech_ms, ratio)
            start = max(0, self.first_speech * VAD_WINDOW_BYTES - SPEECH_PAD_BYTES)
            end = min(len(audio), (self.last_speech + 1) * VAD_WINDOW_BYTES + SPEECH_PAD_BYTES)
            audio = audio[start:end]
        started = time.perf_counter()
        text = (await self.services.asr.transcribe(
            np.frombuffer(audio, "<i2").astype(np.float32) / 32768.0, None
        )).strip()
        asr_ms = (time.perf_counter() - started) * 1000
        log.info("voice turn=%d speech_ms=%d ratio=%.2f asr_ms=%.0f text=%r",
                 turn_id, speech_ms, ratio, asr_ms, text)
        if (
            self._vad_enabled()
            and speech_ms < HALLUCINATION_MAX_SPEECH_MS
            and _normalize(text) in HALLUCINATION_FILLERS
        ):
            return await self._ignore(turn_id, "hallucination", speech_ms, ratio)
        await self.push_frame(_event(
            "transcript", turn_id=turn_id, text=text, model=self.model, asr_ms=round(asr_ms, 1),
            vad=self._vad_state(),
        ))

    async def _semantic_stop(self, turn_id: int) -> None:
        try:
            while self.active and turn_id == self.turn_id:
                elapsed_ms = (time.monotonic() - self.stop_started) * 1000
                action = await self.controller.update(self.latest_text, {
                    "stop_requested": True,
                    "silence_ms": elapsed_ms,
                    "elapsed_ms": elapsed_ms,
                    "agent_state": "listening",
                    "session_state": "manual",
                    "revision": self.partial_revision,
                })
                if action == TurnAction.END_TURN:
                    await self._finalize()
                    return
                if action == TurnAction.IGNORE:
                    self.active = False
                    self.audio.clear()
                    await self.push_frame(_event("done", turn_id=turn_id, text="", timings={}))
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - semantic failure falls back to final ASR
            await self._finalize()
        finally:
            self.stop_task = None

    async def _request_stop(self) -> None:
        if not self.active:
            await self.push_frame(_event("error", error={"code": "empty_audio", "message": "no active turn"}))
            return
        if self.controller is None:
            await self._finalize()
            return
        if self.stop_task is None:
            self.stop_started = time.monotonic()
            self.stop_task = asyncio.create_task(self._semantic_stop(self.turn_id))

    async def _begin_turn(self, message: dict, keep_audio: bool = False) -> None:
        await self._cancel_partial()
        await self._cancel_stop()
        self.turn_id += 1
        if self.controller is not None:
            self.controller.reset()
        self.partial_revision = 0
        self.last_partial = 0.0
        self.latest_text = ""
        if not keep_audio:
            self.audio.clear()
            self._reset_vad()
        self.model = message.get("model") or self.model
        self.active = True
        await self.push_frame(_event(
            "session", protocol=2, turn_id=self.turn_id,
            audio_processing=message.get("audio_processing", {}), vad=self._vad_state(),
        ))

    async def _start_barge_in(self, message: dict) -> None:
        self.barge_ignored = False
        if not self.services.settings.voice_barge_in or self.active:
            self.barge_ignored = True
            return
        await self._cancel_partial()
        await self._cancel_stop()
        self.audio.clear()
        self._reset_vad()
        self.barge_pending = True
        self.barge_model = message.get("model") or ""
        self.barge_processing = message.get("audio_processing", {})

    def _barge_speech_ms(self) -> int:
        if self._vad_enabled():
            return self.speech_windows * VAD_WINDOW_MS
        return len(self.audio) // 2 * 1000 // SAMPLE_RATE

    def _reject_barge_in(self) -> None:
        log.info("voice barge-in turn=%d rejected speech_ms=%d", self.turn_id, self._barge_speech_ms())
        self.barge_pending = False
        self.audio.clear()
        self._reset_vad()

    async def _feed_barge_in(self, pcm: bytes) -> None:
        if len(self.audio) + len(pcm) > MAX_PCM_BYTES:
            return
        self.audio.extend(pcm)
        if self._vad_enabled():
            await self._feed_vad(pcm)
            needed = BARGE_IN_MIN_SPEECH_MS
        else:
            needed = BARGE_IN_MIN_AUDIO_MS
        speech_ms = self._barge_speech_ms()
        if speech_ms < needed:
            return
        log.info("voice barge-in turn=%d confirmed speech_ms=%d", self.turn_id, speech_ms)
        self.barge_pending = False
        await self.push_frame(_event("interrupted", turn_id=self.turn_id))
        await self._begin_turn(
            {"model": self.barge_model, "audio_processing": self.barge_processing}, keep_audio=True
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if self.barge_ignored:
                return
            if self.barge_pending:
                await self._feed_barge_in(frame.audio)
                return
            if self.active and len(self.audio) + len(frame.audio) <= MAX_PCM_BYTES:
                await self._cancel_stop()
                self.audio.extend(frame.audio)
                if self._vad_enabled():
                    await self._feed_vad(frame.audio)
                now = time.monotonic()
                if self._should_start_partial(now):
                    self.last_partial = now
                    self.partial_speech_windows = self.speech_windows
                    self.partial_task = asyncio.create_task(
                        self._partial(self.turn_id, bytes(self.audio[-PARTIAL_TAIL_BYTES:]))
                    )
            return
        if not isinstance(frame, InputTransportMessageFrame):
            await self.push_frame(frame, direction)
            return
        message = frame.message
        kind = message.get("type")
        if kind == "start":
            if message.get("barge_in"):
                await self._start_barge_in(message)
            else:
                self.barge_pending = self.barge_ignored = False
                await self._begin_turn(message)
        elif kind == "stop":
            if self.barge_ignored:
                self.barge_ignored = False
            elif self.barge_pending:
                self._reject_barge_in()
            else:
                await self._request_stop()
        elif kind == "cancel":
            self.barge_pending = self.barge_ignored = False
            self.active = False
            self.audio.clear()
            await self._cancel_partial()
            await self._cancel_stop()
            await self.push_frame(_event("cancelled", turn_id=self.turn_id))
        else:
            await self.push_frame(_event(
                "error", error={"code": "unknown_message", "message": f"unknown message type {kind!r}"}
            ))

    async def cleanup(self) -> None:
        await self._cancel_partial()
        await self._cancel_stop()
        await super().cleanup()


class VoiceResponseProcessor(FrameProcessor):
    def __init__(self, services: Services) -> None:
        super().__init__()
        self.services = services
        self.response_task: asyncio.Task | None = None
        self.synth_task: asyncio.Task | None = None
        self.turn_id = 0

    async def _cancel_response(self) -> None:
        tasks = [self.response_task, self.synth_task]
        self.response_task = self.synth_task = None
        for task in tasks:
            if task is not None:
                await self.cancel_task(task)

    async def _play(self, turn_id: int, seq: int, pcm: bytes, tts_ms: float = 0.0) -> None:
        context_id = f"{turn_id}:{seq}"
        await self.push_frame(_ordered_event(
            "tts_start", turn_id=turn_id, sentence_seq=seq, encoding="pcm16",
            sample_rate=16000, channels=1, bytes=len(pcm),
        ))
        await self.push_frame(TTSStartedFrame(context_id=context_id))
        await self.push_frame(TTSAudioRawFrame(
            audio=pcm, sample_rate=16000, num_channels=1, context_id=context_id
        ))
        await self.push_frame(TTSStoppedFrame(context_id=context_id))
        await self.push_frame(_ordered_event(
            "tts_end", turn_id=turn_id, sentence_seq=seq, bytes=len(pcm), tts_ms=round(tts_ms, 1),
        ))

    async def _play_stream(
        self, turn_id: int, seq: int, chunks: AsyncIterator[tuple[bytes, int]]
    ) -> None:
        context_id = f"{turn_id}:{seq}"
        started = time.perf_counter()
        total = 0
        await self.push_frame(_ordered_event(
            "tts_start", turn_id=turn_id, sentence_seq=seq, encoding="pcm16",
            sample_rate=SAMPLE_RATE, channels=1, bytes=0,
        ))
        await self.push_frame(TTSStartedFrame(context_id=context_id))
        async for pcm, rate in chunks:
            pcm = await to_16k(pcm, rate)
            if not pcm:
                continue
            total += len(pcm)
            await self.push_frame(TTSAudioRawFrame(
                audio=pcm, sample_rate=SAMPLE_RATE, num_channels=1, context_id=context_id
            ))
        await self.push_frame(TTSStoppedFrame(context_id=context_id))
        total += -total % CHUNK_BYTES
        tts_ms = (time.perf_counter() - started) * 1000
        await self.push_frame(_ordered_event(
            "tts_end", turn_id=turn_id, sentence_seq=seq, bytes=total, tts_ms=round(tts_ms, 1),
        ))

    async def _synth_worker(
        self, turn_id: int, sentences: asyncio.Queue, tts_total: list[float], failure: list[Exception]
    ) -> None:
        # The task manager swallows task exceptions, so failures are handed back via `failure`.
        seq = 0
        try:
            while (sentence := await sentences.get()) is not None:
                started = time.perf_counter()
                stream = getattr(self.services.tts, "synthesize_stream", None)
                if stream is not None:
                    await self._play_stream(turn_id, seq, stream(sentence))
                else:
                    wav = await self.services.tts.synthesize(sentence)
                    pcm, rate = wav_to_pcm16(wav)
                    pcm = pad_to_chunk(await to_16k(pcm, rate))
                    await self._play(turn_id, seq, pcm)
                elapsed = (time.perf_counter() - started) * 1000
                tts_total[0] += elapsed
                seq += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced by _respond as an in-band error
            log.warning("voice TTS failed turn=%d sentence=%d text=%r: %s", turn_id, seq, sentence, exc)
            failure.append(exc)

    async def _respond(self, turn_id: int, agen, asr_ms: float, vad: str) -> None:
        parts: list[str] = []
        sentences = SentenceBuffer()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_total = [0.0]
        failure: list[Exception] = []
        started = time.perf_counter()
        ttft_ms = 0.0

        try:
            first_token = await anext(agen)
        except StopAsyncIteration:
            self.response_task = None
            await self.push_frame(_event("done", turn_id=turn_id, text="", timings={}))
            return
        except asyncio.CancelledError:
            self.response_task = None
            with suppress(Exception):
                await agen.aclose()
            raise
        except Exception as exc:  # noqa: BLE001 - report backend failures in-band
            self.response_task = None
            with suppress(Exception):
                await agen.aclose()
            await self.push_frame(_event(
                "error", turn_id=turn_id,
                error={"code": "internal", "message": f"{type(exc).__name__}: {exc}"},
            ))
            return

        async def tokens():
            yield first_token
            async for token in agen:
                yield token

        try:
            self.synth_task = self.create_task(self._synth_worker(turn_id, queue, tts_total, failure))
            async for token in tokens():
                if failure:
                    break
                if not parts:
                    ttft_ms = (time.perf_counter() - started) * 1000
                parts.append(token)
                await self.push_frame(_event("token", turn_id=turn_id, text=token))
                for sentence in sentences.feed(token):
                    queue.put_nowait(sentence)
            if tail := sentences.flush():
                queue.put_nowait(tail)
            llm_ms = (time.perf_counter() - started) * 1000
            queue.put_nowait(None)
            await self.synth_task
            if failure:
                raise failure[0]
            await self.push_frame(_ordered_event(
                "done", turn_id=turn_id, text="".join(parts).strip(),
                timings={
                    "asr_ms": asr_ms,
                    "llm_ttft_ms": round(ttft_ms, 1),
                    "llm_ms": round(llm_ms, 1),
                    "tts_ms": round(tts_total[0], 1),
                },
                vad=vad,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report backend failures in-band
            await self.push_frame(_event(
                "error", turn_id=turn_id,
                error={"code": "internal", "message": f"{type(exc).__name__}: {exc}"},
            ))
        finally:
            if agen is not None:
                with suppress(Exception):
                    await agen.aclose()
            synth, self.synth_task = self.synth_task, None
            if synth is not None and not synth.done():
                await self.cancel_task(synth)
            self.response_task = None

    async def cleanup(self) -> None:
        await self._cancel_response()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)) and frame.message.get("type") == "transcript":
            message = frame.message
            self.turn_id = message["turn_id"]
            text = message.get("text", "")
            await self._cancel_response()
            if text:
                agen = self.services.chat.stream(message.get("model", "qwen3-vl"), [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ], CAP_VOICE)
                await self.push_frame(frame, direction)
                self.response_task = self.create_task(self._respond(
                    self.turn_id, agen, message.get("asr_ms", 0.0), message.get("vad", "off")
                ))
                return
            else:
                await self.push_frame(_event("done", turn_id=self.turn_id, text="", timings={}))
        elif isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)) and frame.message.get("type") in {"cancelled", "interrupted"}:
            self.turn_id = frame.message["turn_id"]
            await self._cancel_response()
            await self.broadcast_interruption()
        await self.push_frame(frame, direction)
