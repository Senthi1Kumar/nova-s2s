"""One OpenAI-realtime-shaped WebSocket session over NovaPipeline streaming cascade.

Callers: nova_hailo/web/app.py websocket /v1/realtime.
WS event schema: {type, audio|delta|transcript|response|bench|generation_id}.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import threading
import time
import uuid
from typing import Any

import numpy as np
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from nova_hailo.audio_gate import accept_utterance
from nova_hailo.bench.session_tracker import BenchTracker
from nova_hailo.metrics import Timer
from nova_hailo.pipeline import NovaPipeline
from nova_hailo.pvad_optional import OptionalFireRedPVAD
from nova_hailo.session_fsm import SessionFSM, SessionState, TurnTerminal
from nova_hailo.voice_loop import prepare_for_whisper
from nova_hailo.wake_kws import WakeWordDetector
from nova_hailo.web.vad import create_vad_segmenter


def _pcm16_b64(audio: np.ndarray) -> str:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    i16 = (x * 32767.0).astype(np.int16)
    return base64.b64encode(i16.tobytes()).decode("ascii")


def _b64_pcm16(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii") if isinstance(data, str) else data)


class RealtimeSession:
    """Generation-safe realtime session with explicit FSM + bounded pending queue."""

    def __init__(
        self,
        websocket: WebSocket,
        pipeline: NovaPipeline,
        bench: BenchTracker,
        loop: asyncio.AbstractEventLoop,
    ):
        self.ws = websocket
        self.pipeline = pipeline
        self.bench = bench
        self.loop = loop
        self.session_id = "sess_" + uuid.uuid4().hex[:12]
        cfg = pipeline.cfg
        idle = float(cfg.get("pipeline", "session_idle_sec", default=45) or 45)
        self.fsm = SessionFSM(session_idle_sec=idle)
        self.vad = create_vad_segmenter()
        self._turn_lock = threading.Lock()
        self._closed = False
        self._response_id: str | None = None
        self._pending: collections.deque[np.ndarray] = collections.deque(maxlen=2)
        self._ptt_armed = False
        wake_mode = str(cfg.get("wake", "mode", default="ptt") or "ptt").lower()
        self.wake = WakeWordDetector(
            enabled=bool(cfg.get("wake", "kws_enabled", default=False)),
            threshold=float(cfg.get("wake", "kws_threshold", default=0.65) or 0.65),
            model_path=cfg.get("wake", "kws_model"),
        )
        self.require_arm = wake_mode in {"kws", "ptt"} and (
            bool(cfg.get("wake", "kws_enabled", default=False)) or wake_mode == "ptt"
        )
        if wake_mode == "ptt" and not self.wake.available:
            self.require_arm = True
        self.pvad = OptionalFireRedPVAD(
            enabled=bool(cfg.get("pvad", "enabled", default=False)),
            model_dir=cfg.get("pvad", "model_dir"),
            threshold=float(cfg.get("pvad", "barge_in_threshold", default=0.55) or 0.55),
            required_frames=int(cfg.get("pvad", "barge_in_frames", default=2) or 2),
        )
        cfgv = pipeline.cfg
        self.barge_in_while_speaking = bool(
            cfgv.get("voice", "barge_in_while_speaking", default=False)
        )
        self.echo_tail_ms = int(cfgv.get("voice", "echo_tail_ms", default=400) or 400)
        self._echo_until = 0.0
        self._echo_logged = False
        self.echo_max_ms = int(cfgv.get("voice", "echo_max_ms", default=12000) or 12000)
        self._playback_stop_ack_at: float | None = None
        self._first_audio_ack_at: float | None = None

    async def send(self, event: dict[str, Any]):
        if self._closed or self.ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await self.ws.send_text(json.dumps(event))
        except Exception:
            pass

    def _schedule_send(self, event: dict[str, Any]):
        if self._closed:
            return
        asyncio.run_coroutine_threadsafe(self.send(event), self.loop)

    async def run(self):
        await self.ws.accept()
        self.fsm.arm()
        self.fsm.begin_listen()
        await self.send(
            {
                "type": "session.created",
                "session": {
                    "id": self.session_id,
                    "type": "realtime",
                    "model": "nova-hailo",
                    "wake": self.wake.status(),
                    "pvad": self.pvad.status(),
                    "fsm": self.fsm.snapshot(),
                },
            }
        )
        try:
            while True:
                raw = await self.ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle_client(msg)
        except WebSocketDisconnect:
            pass
        finally:
            self._closed = True
            self.pipeline.on_abort()

    async def _handle_client(self, msg: dict[str, Any]):
        mtype = msg.get("type")
        if mtype == "session.update":
            session = msg.get("session") or {}
            audio = session.get("audio") or {}
            inp = audio.get("input") or {}
            td = inp.get("turn_detection") or {}
            thr = td.get("threshold")
            if thr is not None:
                self.vad.set_threshold(float(thr))
            if session.get("arm") or session.get("ptt"):
                self._ptt_armed = True
                self.fsm.arm()
                self.fsm.begin_listen()
                await self.send({"type": "session.armed", "fsm": self.fsm.snapshot()})
            return

        if mtype == "input_audio_buffer.arm" or mtype == "ptt.down":
            self._ptt_armed = True
            self.fsm.arm()
            self.fsm.begin_listen()
            await self.send({"type": "session.armed", "fsm": self.fsm.snapshot()})
            return

        if mtype == "playback.started":
            self._first_audio_ack_at = time.perf_counter()
            ctx = self.fsm.current
            if ctx is not None and ctx.first_audio_played_at is None:
                ctx.first_audio_played_at = self._first_audio_ack_at
            return

        if mtype == "playback.interrupted" or mtype == "playback.stopped":
            self._playback_stop_ack_at = time.perf_counter()
            self._echo_until = 0.0  # audible output is over; reopen the mic
            ctx = self.fsm.current
            if ctx is not None and ctx.barge_in_at is not None:
                stop_ms = (self._playback_stop_ack_at - ctx.barge_in_at) * 1000
                ctx.metadata["barge_in_stop_ms"] = stop_ms
            return

        if mtype == "response.cancel":
            # Client fires cancel on every speech_started; only barge when busy.
            # Ignore it inside the echo window: the browser's own VAD also trips
            # on Nova's playback, which would re-open the self-barge path.
            if (
                not self.barge_in_while_speaking
                and time.monotonic()
                < getattr(self, "_echo_until", 0.0) + self.echo_tail_ms / 1000.0
            ):
                return
            if self.fsm.state in {
                SessionState.TRANSCRIBING,
                SessionState.THINKING,
                SessionState.SPEAKING,
            } or self.pipeline.tts.is_speaking:
                self._barge_in(reason="client_cancel")
            return

        if mtype == "input_audio_buffer.append":
            audio_b64 = msg.get("audio")
            if not audio_b64:
                return
            pcm = _b64_pcm16(audio_b64)
            self.fsm.maybe_expire_arm()

            if self.wake.enabled and self.wake.process(pcm):
                self.fsm.arm()
                self.fsm.begin_listen()
                self._schedule_send({"type": "wake.detected", "fsm": self.fsm.snapshot()})

            speaking = self.fsm.state == SessionState.SPEAKING or self.pipeline.tts.is_speaking
            if speaking and self.pvad.available and self.pvad.feed_pcm16(pcm):
                self._barge_in(reason="pvad_target_speech")

            # Echo guard: the speaker feeds the mic, so while Nova is speaking the
            # VAD sees Nova's own voice, barges in, then starts a fresh turn from
            # that audio — self-conversation plus chopped playback. Browser AEC
            # does not cancel it on this PipeWire path and pVAD (the proper
            # speaker check) is off by default. Unless barge-in is explicitly
            # enabled, drop VAD events while speaking and through a decay tail.
            tail = self.echo_tail_ms / 1000.0
            in_echo_window = time.monotonic() < (getattr(self, "_echo_until", 0.0) + tail)
            if (speaking or in_echo_window) and not self.barge_in_while_speaking:
                self.vad.feed(pcm)  # keep VAD state coherent, discard events
                if not self._echo_logged:
                    remain = max(0.0, self._echo_until - time.monotonic())
                    print(f"[echo] mic gated during playback ({remain:.1f}s left)")
                    self._echo_logged = True
                return
            self._echo_logged = False

            for ev, payload in self.vad.feed(pcm):
                if ev == "speech_started":
                    await self.send({"type": "input_audio_buffer.speech_started"})
                    if speaking or self.fsm.state in {
                        SessionState.THINKING,
                        SessionState.SPEAKING,
                        SessionState.TRANSCRIBING,
                    }:
                        self._barge_in(reason="vad_speech_started")
                    else:
                        self.fsm.begin_listen()
                elif ev == "speech_stopped" and payload is not None:
                    await self.send({"type": "input_audio_buffer.speech_stopped"})
                    self._start_turn(payload)
            return

        if mtype == "input_audio_buffer.commit":
            return

    def _barge_in(self, reason: str):
        ctx = self.fsm.interrupt(reason=reason)
        self.pipeline.on_abort()
        # Playback is being dropped, so stop gating the mic immediately.
        self._echo_until = 0.0
        self._schedule_send(
            {
                "type": "response.done",
                "response": {
                    "id": self._response_id or "resp_abort",
                    "status": "cancelled",
                    "metadata": {
                        "barge_in": True,
                        "reason": reason,
                        "generation_id": self.fsm.generation_id,
                        "barge_in_stop_ms": (ctx.metadata.get("barge_in_stop_ms") if ctx else None),
                    },
                },
            }
        )
        self._schedule_send(
            {
                "type": "playback.cancel",
                "generation_id": self.fsm.generation_id,
                "reason": reason,
            }
        )

    def _start_turn(self, audio: np.ndarray):
        if not self._turn_lock.acquire(blocking=False):
            self._pending.append(audio)
            return

        def worker():
            try:
                self._run_turn_blocking(audio)
            finally:
                self._turn_lock.release()
                if self._pending and not self._closed:
                    nxt = self._pending.popleft()
                    self._start_turn(nxt)

        threading.Thread(target=worker, daemon=True, name="realtime-turn").start()

    def _run_turn_blocking(self, audio: np.ndarray):
        self.pipeline.begin_turn()
        self._first_audio_ack_at = None
        self._playback_stop_ack_at = None
        ctx = self.fsm.begin_turn()
        generation_id = ctx.generation_id
        self.pipeline.active_generation_id = generation_id
        self._response_id = "resp_" + uuid.uuid4().hex[:10]
        wall0 = time.perf_counter()
        speech_stopped_at = ctx.speech_stopped_at or wall0

        ok, reason, st = accept_utterance(audio, sample_rate=16000)
        if not ok:
            print(f"[asr] gate reject: {reason} stats={st}")
            self._schedule_send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "",
                    "generation_id": generation_id,
                }
            )
            self.fsm.complete(
                generation_id,
                TurnTerminal.REJECTED,
                metadata={"reason": reason, **st},
            )
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "completed",
                        "metadata": {"skipped": True, "reason": reason, **st},
                    },
                }
            )
            return

        if not self.fsm.is_current(generation_id):
            return

        prepared, _info = prepare_for_whisper(audio, src_sr=16000)

        t_load = Timer()
        stt = self.pipeline._ensure_stt()
        load_ms = t_load.ms()
        try:
            t_inf = Timer()

            def _stt():
                return stt.transcribe(prepared)

            transcript = self.pipeline._with_genai(_stt)
            infer_ms = t_inf.ms()
        finally:
            if self.pipeline.sequential_stt:
                self.pipeline._release_stt()
        stt_ms = load_ms + infer_ms

        if not self.fsm.is_current(generation_id):
            return

        self._schedule_send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": transcript or "",
                "generation_id": generation_id,
            }
        )
        if not transcript:
            metrics = {
                "stt_ms": round(stt_ms, 1),
                "stt_load_ms": round(load_ms, 1),
                "stt_infer_ms": round(infer_ms, 1),
                "total_latency_ms": round((time.perf_counter() - wall0) * 1000, 1),
                "generation_id": generation_id,
                "notes": ["empty transcript"],
            }
            self.bench.add_turn(metrics)
            self.fsm.complete(generation_id, TurnTerminal.REJECTED, metadata=metrics)
            self._schedule_send({"type": "nova.turn_metrics", **metrics})
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "completed",
                        "metadata": {},
                    },
                }
            )
            return

        self.fsm.set_thinking(generation_id)

        def on_pcm(chunk: np.ndarray, sample_rate: int):
            if not self.fsm.is_current(generation_id):
                return
            self.fsm.set_speaking(generation_id)
            if ctx.first_pcm_sent_at is None:
                ctx.first_pcm_sent_at = time.perf_counter()
            # Extend the echo window by the audio duration we just handed the
            # browser. tts.is_speaking only covers server-side synthesis (~250ms)
            # while the browser plays for seconds, so without this the guard is
            # open for most of the audible period and Nova hears itself.
            dur = len(np.asarray(chunk).reshape(-1)) / float(max(sample_rate, 1))
            now_m = time.monotonic()
            self._echo_until = min(
                max(self._echo_until, now_m) + dur, now_m + self.echo_max_ms / 1000.0
            )
            self._schedule_send(
                {
                    "type": "response.audio.delta",
                    "delta": _pcm16_b64(chunk),
                    "sample_rate": int(sample_rate),
                    "generation_id": generation_id,
                }
            )

        def on_clause(text: str):
            if not self.fsm.is_current(generation_id):
                return
            self._schedule_send(
                {
                    "type": "response.audio_transcript.delta",
                    "delta": text,
                    "generation_id": generation_id,
                }
            )

        def on_research_status(payload: dict):
            if not self.fsm.is_current(generation_id):
                return
            self._schedule_send(
                {
                    "type": "nova.research_status",
                    "job_id": payload.get("job_id"),
                    "status": payload.get("status"),
                    "generation_id": generation_id,
                }
            )

        self.pipeline.on_audio_pcm = on_pcm
        self.pipeline.on_validated_clause = on_clause
        self.pipeline.on_research_status = on_research_status
        self.pipeline.on_token = None
        try:
            if not self.fsm.is_current(generation_id):
                return
            result = self.pipeline.run_text_turn(
                transcript,
                stt_ms=stt_ms,
                wall0=wall0,
                record_session=True,
                begin=False,
            )
            if not self.fsm.is_current(generation_id):
                return
            result.metrics.stt_load_ms = load_ms
            result.metrics.stt_infer_ms = infer_ms
            result.metrics.generation_id = generation_id
            if ctx.first_pcm_sent_at is not None:
                result.metrics.speech_end_to_first_pcm_ms = (
                    ctx.first_pcm_sent_at - speech_stopped_at
                ) * 1000
            # True TTFA is when the browser reports sound leaving the speaker.
            # Fall back to the send timestamp only when there is no ack (local
            # playback, or it has not landed yet), and label which one was used
            # so an optimistic number is never mistaken for the real thing.
            if ctx.first_audio_played_at is not None:
                result.metrics.speech_end_to_audible_ms = (
                    ctx.first_audio_played_at - speech_stopped_at
                ) * 1000
                result.metrics.audible_source = "playback_ack"
            else:
                result.metrics.speech_end_to_audible_ms = (
                    result.metrics.speech_end_to_first_pcm_ms
                )
                result.metrics.audible_source = "first_pcm"
            if self._first_audio_ack_at is not None and ctx.first_pcm_sent_at is not None:
                result.metrics.playback_ack_ms = (
                    self._first_audio_ack_at - ctx.first_pcm_sent_at
                ) * 1000
            cancelled = self.pipeline.abort_event.is_set() or (
                ctx.terminal == TurnTerminal.CANCELLED
            )
            if cancelled:
                result.metrics.barge_in = True
                if ctx.barge_in_at is not None:
                    result.metrics.barge_in_at_ms = (ctx.barge_in_at - wall0) * 1000
                stop_ms = ctx.metadata.get("barge_in_stop_ms")
                if stop_ms is not None:
                    result.metrics.barge_in_stop_ms = float(stop_ms)

            metrics = result.metrics.to_dict()
            if result.assistant_text and not cancelled:
                self._schedule_send(
                    {
                        "type": "response.audio_transcript.done",
                        "transcript": result.assistant_text,
                        "generation_id": generation_id,
                    }
                )

            self.bench.add_turn(metrics)
            terminal = TurnTerminal.CANCELLED if cancelled else TurnTerminal.COMPLETED
            self.fsm.complete(generation_id, terminal, metadata=metrics)
            self._schedule_send({"type": "nova.turn_metrics", **metrics})
            self._schedule_send(
                {
                    "type": "nova.metrics",
                    "bench": self.bench.summary(),
                    "turn": metrics,
                    "fsm": self.fsm.snapshot(),
                }
            )
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "cancelled" if cancelled else "completed",
                        "metadata": {
                            "decode_tok_s": metrics.get("decode_tok_s"),
                            "ttfa_ms": metrics.get("ttfa_ms"),
                            "audible_source": metrics.get("audible_source"),
                            "speech_end_to_first_pcm_ms": metrics.get(
                                "speech_end_to_first_pcm_ms"
                            ),
                            "speech_end_to_audible_ms": metrics.get(
                                "speech_end_to_audible_ms"
                            ),
                            "overlap_ms": metrics.get("overlap_ms"),
                            "barge_in": metrics.get("barge_in"),
                            "barge_in_stop_ms": metrics.get("barge_in_stop_ms"),
                            "generation_id": generation_id,
                        },
                    },
                }
            )
        finally:
            self.pipeline.on_audio_pcm = None
            self.pipeline.on_validated_clause = None
            self.pipeline.on_research_status = None
            self.pipeline.on_token = None
            self.pvad.reset()
