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

from nova_hailo.audio_gate import (
    DEFAULT_MIN_PEAK,
    DEFAULT_MIN_RMS,
    DEFAULT_MIN_SEC,
    DEFAULT_MIN_SPEECH_FRAC,
    accept_utterance,
)
from nova_hailo.bench.session_tracker import BenchTracker
from nova_hailo.metrics import Timer, TurnMetrics
from nova_hailo.pipeline import NovaPipeline
from nova_hailo.session_log import current as session_log_current
from nova_hailo.web.dtln import DEFAULT_STRENGTH, create_dtln
from nova_hailo.pvad_optional import OptionalFireRedPVAD
from nova_hailo.session_fsm import SessionFSM, SessionState, TurnTerminal
from nova_hailo.voice_loop import prepare_for_whisper
from nova_hailo.wake_kws import WakeWordDetector
from nova_hailo.web.fail_closed import (
    FAIL_CLOSED_COOLDOWN_S,
    FAIL_CLOSED_SPEAK,
    should_speak_fail_closed,
)
from nova_hailo.web.nemo_wait_budget import (
    DEFAULT_NEMO_WAIT_BASE_S,
    DEFAULT_NEMO_WAIT_CAP_S,
    DEFAULT_NEMO_WAIT_FLOOR_S,
    DEFAULT_NEMO_WAIT_SCALE,
    nemo_wait_budget_s,
)

from nova_hailo.web.vad import create_vad_segmenter

# How often to check for a finished async research job. Short enough that a
# result feels prompt, long enough to cost nothing when there is no job.
RESEARCH_TICK_SEC = 2.0
# Speaking a finished result is safe in either quiet state. ARMED matters most:
# fsm.arm() parks the session there after every turn, and with a quiet user the
# ARMED->IDLE transition -- evaluated only when something calls into the FSM --
# may never happen at all.
_RESEARCH_QUIET_STATES = frozenset({SessionState.IDLE, SessionState.ARMED})


def _transcript_is_echo(user: str, last_assistant: str) -> bool:
    """True when ASR likely captured Nova's own last speak-back."""
    u = (user or "").strip().lower()
    prev = (last_assistant or "").strip().lower()
    if not prev or len(u) < 4:
        return False
    pw = {w for w in prev.replace("'", "").split() if len(w) > 2}
    uw = {w for w in u.replace("'", "").split() if len(w) > 2}
    if not uw:
        return False
    overlap = len(pw & uw) / max(1, len(uw))
    return overlap >= 0.55 or u in prev or prev[:24] in u


def _pcm16_b64(audio: np.ndarray) -> str:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    i16 = (x * 32767.0).astype(np.int16)
    return base64.b64encode(i16.tobytes()).decode("ascii")


def _b64_pcm16(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii") if isinstance(data, str) else data)


def _integrations_snapshot() -> dict[str, Any]:
    """Read-only Google + connector status for the QML driver face.

    The QML driver face only ever shows this, never configures it (that stays
    in the browser settings UI), so a stale or missing value must never block
    a turn. Each lookup is independently wrapped: a failure in one must not
    take out the other, and any failure falls back to a disconnected/empty
    state rather than raising.
    """
    google = {"connected": False, "needs_reauth": False}
    try:
        from nova_hailo.google_oauth import integration_status

        status = integration_status() or {}
        needs_reauth = bool(status.get("needs_reauth"))
        google = {
            "connected": bool(status.get("authenticated")) and not needs_reauth,
            "needs_reauth": needs_reauth,
        }
    except Exception:
        pass

    connectors = {"enabled": 0, "tools": 0}
    try:
        from nova_hailo.connectors.registry import list_connectors, total_enabled_tools

        enabled = sum(1 for c in (list_connectors() or []) if c.get("enabled"))
        connectors = {"enabled": enabled, "tools": int(total_enabled_tools() or 0)}
    except Exception:
        pass

    return {"google": google, "connectors": connectors}


def _google_connect_payload() -> dict[str, Any]:
    """Kick off (or resume) the Google OAuth flow for a websocket client.

    Read-only from the QML driver face's perspective: it only ever receives
    this reply and opens ``auth_url`` in a real browser -- an OAuth consent
    screen cannot render usefully on the driver display. Mirrors the REST
    ``/integrations/google/auth/start`` handler in web/app.py so both
    surfaces share one behavior. Any failure to start the flow degrades to
    an error reply instead of raising into the WS reader loop.
    """
    from nova_hailo.google_oauth import begin_ui_oauth_flow

    try:
        result = begin_ui_oauth_flow() or {}
    except Exception as exc:  # noqa: BLE001
        return {"type": "nova.google.auth_url", "auth_url": None, "error": str(exc)}
    return {
        "type": "nova.google.auth_url",
        "auth_url": result.get("auth_url"),
        "status": result.get("status"),
    }


def _google_disconnect_payload() -> dict[str, Any]:
    """Forget stored refresh tokens locally (does not revoke at Google).

    Mirrors the REST ``/integrations/google/disconnect`` handler. Never
    raises: a corrupt or unreadable token store just means there was nothing
    to forget.
    """
    from nova_hailo.google_oauth import GoogleTokenProvider

    try:
        GoogleTokenProvider().store.clear()
    except Exception:
        pass
    return {"type": "nova.google.disconnected"}


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
        try:
            self.fsm = SessionFSM(session_idle_sec=idle, on_change=self._on_fsm_change)
        except TypeError:
            # Older SessionFSM without on_change — still accept the WS.
            self.fsm = SessionFSM(session_idle_sec=idle)
        self.vad = create_vad_segmenter()
        self.ns = create_dtln(cfg)
        self._ns_held = False
        self._turn_lock = threading.Lock()
        self._closed = False
        self._response_id: str | None = None
        self._pending: collections.deque[tuple[np.ndarray, dict | None]] = collections.deque(maxlen=2)
        self._nemo_ws = None
        self._nemo_ws_task: asyncio.Task | None = None
        self._research_task: asyncio.Task | None = None
        self._nemo_stream_result: dict | None = None
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
        # Energy gate knobs (audio_gate.accept_utterance); defaults match module constants.
        self.gate_min_rms = float(
            cfgv.get("voice", "gate_min_rms", default=DEFAULT_MIN_RMS) or DEFAULT_MIN_RMS
        )
        self.gate_min_peak = float(
            cfgv.get("voice", "gate_min_peak", default=DEFAULT_MIN_PEAK) or DEFAULT_MIN_PEAK
        )
        self.gate_min_sec = float(
            cfgv.get("voice", "gate_min_sec", default=DEFAULT_MIN_SEC) or DEFAULT_MIN_SEC
        )
        self.gate_min_speech_frac = float(
            cfgv.get("voice", "gate_min_speech_frac", default=DEFAULT_MIN_SPEECH_FRAC)
            or DEFAULT_MIN_SPEECH_FRAC
        )
        # Nemo sidecar post-commit wait budget (voice.nemo_wait_*).
        self.nemo_wait_floor_s = float(
            cfgv.get("voice", "nemo_wait_floor_s", default=DEFAULT_NEMO_WAIT_FLOOR_S)
            or DEFAULT_NEMO_WAIT_FLOOR_S
        )
        self.nemo_wait_cap_s = float(
            cfgv.get("voice", "nemo_wait_cap_s", default=DEFAULT_NEMO_WAIT_CAP_S)
            or DEFAULT_NEMO_WAIT_CAP_S
        )
        self.nemo_wait_base_s = float(
            cfgv.get("voice", "nemo_wait_base_s", default=DEFAULT_NEMO_WAIT_BASE_S)
            or DEFAULT_NEMO_WAIT_BASE_S
        )
        self.nemo_wait_scale = float(
            cfgv.get("voice", "nemo_wait_scale", default=DEFAULT_NEMO_WAIT_SCALE)
            or DEFAULT_NEMO_WAIT_SCALE
        )
        self._playback_stop_ack_at: float | None = None
        self._first_audio_ack_at: float | None = None
        self._last_fail_closed_mono: float = float("-inf")
        self._echo_hold_started: float = 0.0
        self._uplink_frames: int = 0
        self._uplink_log_at: float = 0.0

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

    def _voice_snapshot(self) -> dict[str, Any]:
        ns = self.ns
        strength = float(getattr(ns, "strength", DEFAULT_STRENGTH) or DEFAULT_STRENGTH) if ns else 0.0
        return {
            "gate_min_rms": self.gate_min_rms,
            "ns": "dtln" if ns is not None else "off",
            "ns_strength": strength,
            "stt_engine": getattr(self.pipeline, "stt_engine", None),
        }

    def _settings_payload(self) -> dict[str, Any]:
        snap = {}
        fn = getattr(self.pipeline, "llm_settings_snapshot", None)
        if fn is not None:
            try:
                snap = fn() or {}
            except Exception:
                snap = {}
        snap.update(self._voice_snapshot())
        snap.update(_integrations_snapshot())
        return snap

    def _apply_voice_settings(self, payload: dict[str, Any]) -> None:
        if payload.get("gate_min_rms") is not None:
            self.gate_min_rms = max(0.002, min(0.12, float(payload["gate_min_rms"])))
        ns_name = payload.get("ns")
        if ns_name is not None:
            want = str(ns_name).strip().lower()
            if want in {"off", "none", "0", "false"}:
                self.ns = None
                print("[dtln] disabled via settings", flush=True)
            elif self.ns is None:
                self.ns = create_dtln(self.pipeline.cfg)
        if payload.get("ns_strength") is not None and self.ns is not None:
            self.ns.strength = max(0.0, min(1.0, float(payload["ns_strength"])))
            print(f"[dtln] mix={self.ns.strength}", flush=True)

    async def _apply_llm_settings_bg(self, payload: dict[str, Any]) -> None:
        apply = getattr(self.pipeline, "apply_llm_settings", None)
        if apply is None:
            await self.send(
                {
                    "type": "nova.llm_status",
                    "status": "error",
                    "error": "llm settings not supported",
                }
            )
            return
        try:
            snap = await asyncio.to_thread(
                lambda: apply(
                    mode=payload.get("mode"),
                    local_hef=payload.get("local_hef"),
                    or_model=payload.get("or_model"),
                )
            )
            await self.send(
                {
                    "type": "nova.settings",
                    **{**snap, **self._voice_snapshot(), **_integrations_snapshot()},
                }
            )
            await self.send({"type": "nova.llm_status", "status": "ready"})
        except Exception as exc:  # noqa: BLE001
            await self.send(
                {"type": "nova.llm_status", "status": "error", "error": str(exc)}
            )

    def _on_fsm_change(self, snapshot: dict[str, Any]) -> None:
        """Push every FSM transition to the browser (driver orb + /dashboard)."""
        self._schedule_send(
            {
                "type": "nova.fsm",
                "fsm": snapshot,
                "session_id": self.session_id,
                "stt_engine": getattr(self.pipeline, "stt_engine", None),
            }
        )
        # Deliver any finished async research strictly on IDLE entry -- never
        # while a real turn is anywhere in flight. This callback runs inside
        # SessionFSM's own lock (see SessionFSM.complete/_emit), so it must
        # only ever schedule work, never do it inline.
        if snapshot.get("state") == SessionState.IDLE.value:
            self._maybe_deliver_research()

    def _maybe_deliver_research(self) -> None:
        """On IDLE entry, speak any finished deep_research result via TTS.

        Never speaks over a user: takes the same ``_turn_lock`` a real
        incoming turn takes, then re-checks IDLE/abort right before draining
        and again before each result is spoken. If any check fails -- lock
        busy, a real turn started, abort_event set -- the delivery is simply
        dropped; losing a result is better than talking over someone. Runs
        on its own thread because speaking blocks on TTS synth/playback and
        this is called from inside the FSM's lock.
        """
        if self._closed:
            return
        if not self.pipeline.pending_research():
            return  # cheap peek; avoids a thread spawn on every idle tick

        def worker() -> None:
            if self._closed or self.pipeline.abort_event.is_set():
                return
            if not self._turn_lock.acquire(blocking=False):
                return  # a real turn is starting/in flight -- never wait for it
            try:
                if self._closed or self.pipeline.abort_event.is_set():
                    return
                if self.fsm.state not in _RESEARCH_QUIET_STATES or self.pipeline.tts.is_speaking:
                    return
                for item in self.pipeline.drain_finished_research():
                    if self._closed or self.pipeline.abort_event.is_set():
                        break
                    if self.fsm.state not in _RESEARCH_QUIET_STATES or self.pipeline.tts.is_speaking:
                        break
                    self._speak_research_result(item)
            finally:
                self._turn_lock.release()

        threading.Thread(target=worker, daemon=True, name="research-deliver").start()

    def _speak_research_result(self, item: dict[str, Any]) -> bool:
        """Speak one finished deep_research result. Returns True if spoken.

        Deliberately does not touch SessionFSM state (stays IDLE throughout):
        every barge-in path in this file (``response.cancel``, pVAD, VAD
        speech_started) already keys off ``self.pipeline.tts.is_speaking`` in
        addition to FSM state, so a real utterance during this delivery still
        interrupts it correctly. Gating instead uses ``pipeline.gen_guard``
        directly, the same primitive ``on_abort()`` invalidates on barge-in.
        """
        speak = str(item.get("speak") or "").strip()
        if not speak:
            return False
        prefixed = f"About that research you asked for — {speak}"
        self.pipeline.begin_turn()  # clear abort/barge-in/gen_guard before speaking
        my_gen = self.pipeline.gen_guard.generation_id

        def on_pcm(chunk: np.ndarray, sample_rate: int) -> None:
            if not self.pipeline.gen_guard.is_live(my_gen):
                return
            # Extend the echo window by the audio duration we just handed the
            # browser -- same as _run_turn_blocking's on_pcm. Without this,
            # the mic reopens before playback actually finishes and the tail
            # of Nova's own answer looks like a fresh utterance.
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
                    "generation_id": my_gen,
                }
            )

        def on_clause(text: str) -> None:
            if not self.pipeline.gen_guard.is_live(my_gen):
                return
            self._schedule_send(
                {
                    "type": "response.audio_transcript.delta",
                    "delta": text,
                    "generation_id": my_gen,
                }
            )

        self.pipeline.on_audio_pcm = on_pcm
        self.pipeline.on_validated_clause = on_clause
        self.pipeline.on_research_status = None
        self.pipeline.on_token = None
        try:
            if self.fsm.state != SessionState.IDLE or self.pipeline.abort_event.is_set():
                return False
            tm = TurnMetrics(generation_id=my_gen, notes=["research_delivery"])
            result = self.pipeline._speak_tool_line(
                prefixed,
                wall0=time.perf_counter(),
                metrics=tm,
                user_text=str(item.get("user_text") or ""),
                auth_decision="n/a",
                tool_name="deep_research",
                record_session=False,
                add_history=True,
                research_job_id=str(item.get("job_id") or ""),
            )
            if not self.pipeline.gen_guard.is_live(my_gen):
                return False  # barged in mid-speech; browser already got playback.cancel
            self._schedule_send(
                {
                    "type": "response.audio_transcript.done",
                    "transcript": prefixed,
                    "generation_id": my_gen,
                }
            )
            out = result.metrics.to_dict()
            self.bench.add_turn(out)
            self._schedule_send({"type": "nova.turn_metrics", **out})
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": "resp_" + uuid.uuid4().hex[:10],
                        "status": "completed",
                        "metadata": {
                            "research_delivery": True,
                            "job_id": item.get("job_id"),
                            "generation_id": my_gen,
                        },
                    },
                }
            )
            return True
        finally:
            self.pipeline.on_audio_pcm = None
            self.pipeline.on_validated_clause = None
            self.pipeline.on_research_status = None
            self.pipeline.on_tool_status = None
            self.pipeline.on_token = None

    async def _research_tick(self) -> None:
        """Poll for finished async research instead of waiting on an IDLE edge.

        ``fsm.arm()`` parks the session in ARMED after every turn and the
        ARMED->IDLE transition is evaluated lazily, only when something calls
        into the FSM. A quiet user therefore produces no transition at all, so
        hanging delivery off the IDLE edge alone meant a finished job was never
        spoken. This ticks regardless; ``_maybe_deliver_research`` still does
        all the never-talk-over-the-user gating.
        """
        try:
            while not self._closed:
                await asyncio.sleep(RESEARCH_TICK_SEC)
                if self._closed:
                    return
                try:
                    self._maybe_deliver_research()
                except Exception as exc:  # noqa: BLE001
                    print(f"[research] tick failed: {type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            pass

    async def run(self):
        await self.ws.accept()
        self.fsm.arm()
        self.fsm.begin_listen()
        self._research_task = asyncio.create_task(self._research_tick())
        greeting = ""
        try:
            mem = getattr(self.pipeline, "memory", None)
            if mem is not None and mem.last_session_fact():
                greeting = self.pipeline.startup_greet()
        except Exception:
            greeting = ""
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
                    "stt_engine": getattr(self.pipeline, "stt_engine", None),
                    "greeting": greeting,
                    "llm": getattr(self.pipeline, "llm_settings_snapshot", lambda: {})(),
                },
            }
        )
        snap = getattr(self.pipeline, "llm_settings_snapshot", None)
        if snap is not None:
            await self.send({"type": "nova.settings", **snap(), **_integrations_snapshot()})
        # Explicit first paint for clients that only listen for nova.fsm
        await self.send(
            {
                "type": "nova.fsm",
                "fsm": self.fsm.snapshot(),
                "session_id": self.session_id,
                "stt_engine": getattr(self.pipeline, "stt_engine", None),
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
            task = self._research_task
            self._research_task = None
            if task is not None:
                task.cancel()
            self.pipeline.on_abort()
            await self._finish_nemo_stream()

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

        if mtype == "nova.settings.get":
            await self.send({"type": "nova.settings", **self._settings_payload()})
            return

        if mtype == "nova.settings.set":
            payload = msg.get("settings") if isinstance(msg.get("settings"), dict) else msg
            voice_only = payload.get("mode") is None and payload.get("or_model") is None
            self._apply_voice_settings(payload)
            if voice_only:
                await self.send({"type": "nova.settings", **self._settings_payload()})
                return
            # Do not await HEF load on the WS reader — that froze the mic uplink.
            await self.send({"type": "nova.llm_status", "status": "loading"})
            asyncio.create_task(self._apply_llm_settings_bg(payload))
            return

        if mtype == "nova.google.connect":
            # begin_ui_oauth_flow() binds a local callback socket -- off the
            # WS reader thread so a busy port never stalls the mic uplink.
            reply = await asyncio.to_thread(_google_connect_payload)
            await self.send(reply)
            await self.send({"type": "nova.settings", **self._settings_payload()})
            return

        if mtype == "nova.google.disconnect":
            reply = _google_disconnect_payload()
            await self.send(reply)
            await self.send({"type": "nova.settings", **self._settings_payload()})
            return

        if mtype in {"input_audio_buffer.arm", "ptt.down", "session.arm"}:
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
            self._uplink_frames += 1
            now_m = time.monotonic()
            if now_m - self._uplink_log_at >= 5.0:
                self._uplink_log_at = now_m
                rms = 0.0
                if pcm:
                    x = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16)
                    if x.size:
                        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)) + 1e-12) / 32768.0
                silent = " silent" if rms < 1e-4 else ""
                print(
                    f"[uplink] frames={self._uplink_frames} bytes={len(pcm)} "
                    f"rms={rms:.4f}{silent} fsm={self.fsm.state.value} "
                    f"speaking={self.pipeline.tts.is_speaking}",
                    flush=True,
                )
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
            echo_cap = self.echo_max_ms / 1000.0
            echo_held_s = max(0.0, time.monotonic() - float(getattr(self, "_echo_hold_started", 0.0) or 0.0))
            if (speaking or in_echo_window) and not getattr(self, "_echo_hold_started", 0.0):
                self._echo_hold_started = time.monotonic()
            if not (speaking or in_echo_window):
                self._echo_hold_started = 0.0
            gated = (speaking or in_echo_window) and not self.barge_in_while_speaking
            if gated and echo_held_s > echo_cap:
                gated = False
                self._echo_until = 0.0
                self._echo_hold_started = 0.0
            if gated:
                self.vad.feed(pcm)  # keep VAD state coherent, discard events
                if self.ns is not None and not self._ns_held:
                    self.ns.reset()
                    self._ns_held = True
                if not self._echo_logged:
                    remain = max(0.0, self._echo_until - time.monotonic())
                    print(f"[echo] mic gated during playback ({remain:.1f}s left)")
                    self._echo_logged = True
                return
            self._echo_logged = False
            self._ns_held = False

            if self.ns is not None:
                pcm = self.ns.process_pcm16(pcm)
                if not pcm:
                    return

            # Process VAD events, but always forward PCM to the sidecar *before*
            # commit. Previous order committed on speech_stopped then tried to
            # send the same frame on a cleared socket — last audio + finish()
            # never paired, so `.completed` often never arrived (5s timeout).
            vad_events = list(self.vad.feed(pcm))
            for ev, payload in vad_events:
                if ev == "speech_started":
                    await self.send({"type": "input_audio_buffer.speech_started"})
                    await self._start_nemo_stream()
                    if speaking or self.fsm.state in {
                        SessionState.THINKING,
                        SessionState.SPEAKING,
                        SessionState.TRANSCRIBING,
                    }:
                        self._barge_in(reason="vad_speech_started")
                    else:
                        self.fsm.begin_listen()

            await self._nemo_send_pcm(pcm)

            for ev, payload in vad_events:
                if ev == "speech_stopped" and payload is not None:
                    await self.send({"type": "input_audio_buffer.speech_stopped"})
                    # Attach utterance duration so the wait budget scales with
                    # speech length instead of a fixed 5s artificial floor.
                    audio_sec = float(np.asarray(payload).reshape(-1).size) / 16000.0
                    nemo_result = await self._finish_nemo_stream(audio_sec=audio_sec)
                    self._start_turn(payload, nemo_result=nemo_result)
                    if self.ns is not None:
                        self.ns.reset()
            return

        if mtype == "input_audio_buffer.commit":
            return

    def _barge_in(self, reason: str):
        ctx = self.fsm.interrupt(reason=reason)
        self.pipeline.on_abort()
        if hasattr(self.pipeline, "gen_guard"):
            self.pipeline.gen_guard.invalidate(reason)
        # Playback is being dropped, so stop gating the mic immediately.
        self._echo_until = 0.0
        # Drop any open sidecar stream so stale finals cannot land on the next turn.
        try:
            loop = self.loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._abort_nemo_stream(), loop)
        except Exception:
            pass
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

    def _nemo_wait_budget_s(self, audio_sec: float) -> float:
        """Post-speech wait for streaming finalization (config-driven knobs)."""
        return nemo_wait_budget_s(
            audio_sec,
            floor=self.nemo_wait_floor_s,
            cap=self.nemo_wait_cap_s,
            base=self.nemo_wait_base_s,
            scale=self.nemo_wait_scale,
        )

    async def _nemo_send_pcm(self, pcm: bytes) -> None:
        """Push one browser PCM16 frame to the sidecar as a binary WS frame.

        Binary little-endian PCM16 is the server's preferred path (docs/server.md);
        base64 JSON append is accepted but heavier and was the only path before.
        """
        ws = self._nemo_ws
        if ws is None:
            return
        try:
            # websockets: send(bytes) → binary frame
            await ws.send(pcm)
        except Exception as exc:  # noqa: BLE001
            # One-line, rate-limited: a closed socket after mid-stream final used
            # to spam dozens of "send failed" lines per second.
            if not getattr(self, "_nemo_send_err_logged", False):
                print(f"[nemo_sidecar] send failed: {exc}")
                self._nemo_send_err_logged = True
            self._nemo_ws = None

    async def _start_nemo_stream(self):
        """Open a WebSocket to nemo-speech serve for the turn about to begin.

        C++ server does the decode (OpenAI-shaped /v1/realtime). Python only
        forwards PCM and waits for the final. No-op when stt_engine != nemo_speech
        or the sidecar is down (offline ctypes path in _run_turn_blocking).
        """
        await self._abort_nemo_stream()
        if self.pipeline.stt_engine != "nemo_speech":
            return
        from nova_hailo.backends.nemo_speech_sidecar import get_sidecar_url

        base = get_sidecar_url()
        if not base:
            return
        import websockets

        ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/v1/realtime"
        try:
            # Per-turn socket. Keepalive pings off: server decode can delay pongs
            # long enough to trip the client (measured: keepalive ping timeout).
            ws = await websockets.connect(
                ws_url, open_timeout=3, close_timeout=2, ping_interval=None
            )
            # Required before first audio (playground + docs/api.md). Without
            # sample_rate=16000 the server may assume the model default incorrectly.
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "sample_rate": 16000,
                            "language": "en",
                            "automatic_punctuation": True,
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[nemo_sidecar] connect failed: {exc}")
            return
        result = {
            "text": "",
            "partial": "",
            "event": threading.Event(),
            "path": "sidecar",
            "audio_sec": 0.0,
        }
        self._nemo_ws = ws
        self._nemo_stream_result = result
        self._nemo_send_err_logged = False
        self._nemo_ws_task = asyncio.create_task(self._nemo_ws_receiver(ws, result))

    async def _nemo_ws_receiver(self, ws, result: dict):
        """Drain sidecar events until a final, commit-ack, or socket death."""
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                mtype = msg.get("type")
                if mtype == "conversation.item.input_audio_transcription.delta":
                    # Cumulative or incremental; prefer full partial when present.
                    delta = msg.get("delta") or ""
                    if msg.get("transcript"):
                        result["partial"] = msg.get("transcript") or ""
                    elif delta:
                        result["partial"] = (result.get("partial") or "") + delta
                elif mtype == "conversation.item.input_audio_transcription.completed":
                    text = (msg.get("transcript") or "").strip()
                    if text:
                        result["text"] = text
                    elif result.get("partial"):
                        result["text"] = result["partial"]
                    # With server endpointing off, the final on commit is the
                    # end of the turn. Break so the wait unblocks immediately.
                    break
                elif mtype == "input_audio_buffer.committed":
                    # finish() can emit empty alternatives (no .completed). Use
                    # the best partial we have and stop waiting.
                    if not result.get("text") and result.get("partial"):
                        result["text"] = result["partial"]
                    break
                elif mtype == "error":
                    print(f"[nemo_sidecar] server error: {msg}")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[nemo_sidecar] receiver ended: {exc}")
        finally:
            result["event"].set()
            try:
                await ws.close()
            except Exception:
                pass
            if self._nemo_ws is ws:
                self._nemo_ws = None

    async def _finish_nemo_stream(self, *, audio_sec: float = 0.0) -> dict | None:
        ws = getattr(self, "_nemo_ws", None)
        result = self._nemo_stream_result
        if result is not None:
            result["audio_sec"] = float(audio_sec)
        if ws is None:
            # Stream already closed (receiver finished) — still return result so
            # the turn can use partial/final text without re-opening.
            self._nemo_stream_result = None
            return result
        self._nemo_ws = None
        self._nemo_stream_result = None
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception as exc:  # noqa: BLE001
            print(f"[nemo_sidecar] commit failed: {exc}")
            if result is not None:
                result["event"].set()
        return result

    async def _abort_nemo_stream(self) -> None:
        """Drop an in-flight sidecar stream without waiting for a final."""
        ws = self._nemo_ws
        result = self._nemo_stream_result
        self._nemo_ws = None
        self._nemo_stream_result = None
        task = self._nemo_ws_task
        self._nemo_ws_task = None
        if result is not None:
            result["event"].set()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if task is not None and not task.done():
            task.cancel()

    def _speak_fail_closed(
        self,
        generation_id: int,
        *,
        reason: str,
        metrics_extra: dict | None = None,
    ) -> None:
        """Speak a fixed fallback when gate/STT fails — never silent, never LLM.

        Callers send ``conversation.item.input_audio_transcription.completed``
        with an empty transcript first. Cooldown within FAIL_CLOSED_COOLDOWN_S
        skips TTS but still completes the turn with response.done.
        """
        print(f"[asr] fail_closed reason={reason}")
        now_m = time.monotonic()
        on_cooldown = not should_speak_fail_closed(
            self._last_fail_closed_mono, now_m, FAIL_CLOSED_COOLDOWN_S
        )

        metrics: dict[str, Any] = dict(metrics_extra or {})
        notes = list(metrics.get("notes") or [])
        if "fail_closed" not in notes:
            notes.append("fail_closed")
        reason_note = f"reason={reason}"
        if reason_note not in notes:
            notes.append(reason_note)
        metrics["generation_id"] = generation_id
        metrics["fail_closed"] = True

        if on_cooldown:
            if "fail_closed_cooldown" not in notes:
                notes.append("fail_closed_cooldown")
            metrics["notes"] = notes
            metrics["reason"] = "fail_closed_cooldown"
            self.bench.add_turn(metrics)
            self.fsm.complete(
                generation_id,
                TurnTerminal.REJECTED,
                metadata=metrics,
            )
            self._schedule_send({"type": "nova.turn_metrics", **metrics})
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "completed",
                        "metadata": {
                            "skipped": True,
                            "fail_closed": True,
                            "reason": "fail_closed_cooldown",
                            "original_reason": reason,
                            "generation_id": generation_id,
                        },
                    },
                }
            )
            return

        self._last_fail_closed_mono = now_m
        metrics["reason"] = reason
        metrics["notes"] = notes
        wall0 = time.perf_counter()
        ctx = self.fsm.current

        def on_pcm(chunk: np.ndarray, sample_rate: int):
            if not self.fsm.is_current(generation_id):
                return
            self.fsm.set_speaking(generation_id)
            if ctx is not None and ctx.first_pcm_sent_at is None:
                ctx.first_pcm_sent_at = time.perf_counter()
            # Extend echo guard for the duration of PCM we hand the browser.
            dur = len(np.asarray(chunk).reshape(-1)) / float(max(sample_rate, 1))
            now_echo = time.monotonic()
            self._echo_until = min(
                max(self._echo_until, now_echo) + dur,
                now_echo + self.echo_max_ms / 1000.0,
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

        self.pipeline.on_audio_pcm = on_pcm
        self.pipeline.on_validated_clause = on_clause
        self.pipeline.on_research_status = None
        self.pipeline.on_token = None
        try:
            if not self.fsm.is_current(generation_id):
                return
            tm = TurnMetrics(generation_id=generation_id, notes=list(notes))
            if metrics_extra:
                for attr in (
                    "stt_ms",
                    "stt_load_ms",
                    "stt_infer_ms",
                    "stt_path",
                    "audio_sec",
                ):
                    if attr in metrics_extra and metrics_extra[attr] is not None:
                        setattr(tm, attr, metrics_extra[attr])
            result = self.pipeline._speak_tool_line(
                FAIL_CLOSED_SPEAK,
                wall0=wall0,
                metrics=tm,
                user_text="",
                auth_decision="n/a",
                tool_name=None,
                record_session=False,
                add_history=False,
            )
            if not self.fsm.is_current(generation_id):
                return
            for n in notes:
                if n not in result.metrics.notes:
                    result.metrics.notes.append(n)
            result.metrics.generation_id = generation_id
            out = result.metrics.to_dict()
            out["fail_closed"] = True
            out["reason"] = reason
            if metrics_extra:
                for k, v in metrics_extra.items():
                    if k == "notes":
                        continue
                    if k not in out or out[k] is None:
                        out[k] = v

            # Clause path already emitted delta via on_validated_clause; finalize.
            self._schedule_send(
                {
                    "type": "response.audio_transcript.done",
                    "transcript": FAIL_CLOSED_SPEAK,
                    "generation_id": generation_id,
                }
            )
            self.bench.add_turn(out)
            self.fsm.complete(
                generation_id,
                TurnTerminal.REJECTED,
                metadata=out,
            )
            self._schedule_send({"type": "nova.turn_metrics", **out})
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "completed",
                        "metadata": {
                            "fail_closed": True,
                            "reason": reason,
                            "generation_id": generation_id,
                            "ttfa_ms": out.get("ttfa_ms"),
                        },
                    },
                }
            )
        finally:
            self.pipeline.on_audio_pcm = None
            self.pipeline.on_validated_clause = None
            self.pipeline.on_research_status = None
            self.pipeline.on_tool_status = None
            self.pipeline.on_token = None

    def _start_turn(self, audio: np.ndarray, nemo_result: dict | None = None):
        # Do not queue speech captured while a turn is in flight — that is the
        # "answers a question I asked seconds ago / I did not speak" loop
        # (echo of TTS + leftover VAD). Barge-in is explicit, not a backlog.
        busy = self.fsm.state in {
            SessionState.TRANSCRIBING,
            SessionState.THINKING,
            SessionState.SPEAKING,
        } or self.pipeline.tts.is_speaking
        if not self._turn_lock.acquire(blocking=False):
            if busy:
                print("[asr] drop overlapping utterance (turn in flight)", flush=True)
                return
            self._pending.append((audio, nemo_result, time.monotonic()))
            return

        def worker():
            try:
                self._run_turn_blocking(audio, nemo_result=nemo_result)
            finally:
                self._turn_lock.release()
                stale_s = 0.35
                while self._pending and not self._closed:
                    item = self._pending.popleft()
                    nxt_audio, nxt_nemo = item[0], item[1]
                    t0 = item[2] if len(item) > 2 else 0.0
                    if t0 and (time.monotonic() - t0) > stale_s:
                        print("[asr] drop stale queued utterance", flush=True)
                        continue
                    self._start_turn(nxt_audio, nemo_result=nxt_nemo)
                    break

        threading.Thread(target=worker, daemon=True, name="realtime-turn").start()

    def _run_turn_blocking(self, audio: np.ndarray, nemo_result: dict | None = None):
        self.pipeline.begin_turn()
        self._first_audio_ack_at = None
        self._playback_stop_ack_at = None
        ctx = self.fsm.begin_turn()
        generation_id = ctx.generation_id
        self.pipeline.active_generation_id = generation_id
        self._response_id = "resp_" + uuid.uuid4().hex[:10]
        wall0 = time.perf_counter()
        speech_stopped_at = ctx.speech_stopped_at or wall0

        ok, reason, st = accept_utterance(
            audio,
            sample_rate=16000,
            min_rms=self.gate_min_rms,
            min_peak=self.gate_min_peak,
            min_sec=self.gate_min_sec,
            min_speech_frac=self.gate_min_speech_frac,
        )
        if not ok:
            print(f"[asr] gate reject: {reason} stats={st}")
            self._schedule_send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "",
                    "generation_id": generation_id,
                }
            )
            self._speak_fail_closed(
                generation_id,
                reason=str(reason),
                metrics_extra={"reason": reason, **st},
            )
            return

        if not self.fsm.is_current(generation_id):
            return

        stt_path = "offline"
        audio_sec = float(np.asarray(audio).reshape(-1).size) / 16000.0
        transcript = ""
        load_ms = 0.0
        infer_ms = 0.0

        if nemo_result is not None:
            # Streaming path: decode overlapped capture. Wait only for the
            # post-commit tail (scaled to speech length), then fall back to
            # offline ctypes if the sidecar produced nothing.
            t_load = Timer()
            load_ms = t_load.ms()
            t_inf = Timer()
            budget = self._nemo_wait_budget_s(
                float(nemo_result.get("audio_sec") or audio_sec)
            )
            if not nemo_result["event"].wait(timeout=budget):
                print(
                    f"[nemo_stream] result wait timed out "
                    f"(budget={budget:.2f}s audio={audio_sec:.2f}s)"
                )
            transcript = (nemo_result.get("text") or nemo_result.get("partial") or "").strip()
            infer_ms = t_inf.ms()
            stt_path = "sidecar" if transcript else "sidecar_empty"
            if not transcript:
                # Critical: previous code treated "sidecar attempted" as final
                # and never fell back — empty text + 5s wait = silent turn.
                print(f"[nemo_stream] empty sidecar result; offline fallback ({audio_sec:.2f}s audio)")
                prepared, _info = prepare_for_whisper(audio, src_sr=16000)
                t_load2 = Timer()
                stt = self.pipeline._ensure_stt()
                load_ms += t_load2.ms()
                try:
                    t_inf2 = Timer()

                    def _stt_fb():
                        return stt.transcribe(prepared)

                    transcript = self.pipeline._with_genai(_stt_fb)
                    infer_ms += t_inf2.ms()
                finally:
                    if self.pipeline.sequential_stt:
                        self.pipeline._release_stt()
                stt_path = "offline_fallback"
        else:
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
            stt_path = "offline"
        stt_ms = load_ms + infer_ms
        print(
            f"[stt] path={stt_path} stt_ms={stt_ms:.0f} audio_s={audio_sec:.2f} "
            f"chars={len(transcript or '')}"
        )

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
            self._speak_fail_closed(
                generation_id,
                reason="empty_transcript",
                metrics_extra={
                    "stt_ms": round(stt_ms, 1),
                    "stt_load_ms": round(load_ms, 1),
                    "stt_infer_ms": round(infer_ms, 1),
                    "stt_path": stt_path,
                    "audio_sec": round(audio_sec, 2),
                    "total_latency_ms": round((time.perf_counter() - wall0) * 1000, 1),
                    "generation_id": generation_id,
                    "notes": [
                        "empty transcript",
                        "fail_closed_empty_stt",
                        f"stt_path={stt_path}",
                    ],
                },
            )
            return

        last_speak = ""
        hist = getattr(self.pipeline, "history", None)
        if hist is not None and getattr(hist, "_turns", None):
            try:
                last_speak = str(hist._turns[-1].assistant or "")
            except Exception:
                last_speak = ""
        if _transcript_is_echo(transcript, last_speak):
            print(f"[asr] skip TTS-echo transcript: {transcript!r}", flush=True)
            self.fsm.complete(generation_id, TurnTerminal.REJECTED, metadata={"echo": True})
            self._schedule_send(
                {
                    "type": "response.done",
                    "response": {
                        "id": self._response_id,
                        "status": "completed",
                        "metadata": {"skipped": True, "reason": "tts_echo", "generation_id": generation_id},
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

        def on_tool_status(payload: dict):
            if not self.fsm.is_current(generation_id):
                return
            self._schedule_send(
                {
                    "type": "nova.tool_status",
                    "name": payload.get("name"),
                    "status": payload.get("status"),
                    "detail": payload.get("detail"),
                    "generation_id": generation_id,
                }
            )

        self.pipeline.on_audio_pcm = on_pcm
        self.pipeline.on_validated_clause = on_clause
        self.pipeline.on_research_status = on_research_status
        self.pipeline.on_tool_status = on_tool_status
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
            result.metrics.stt_path = stt_path
            result.metrics.audio_sec = round(audio_sec, 2)
            if f"stt_path={stt_path}" not in (result.metrics.notes or []):
                result.metrics.notes.append(f"stt_path={stt_path}")
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
            slog = session_log_current()
            if slog is not None:
                llm = getattr(self.pipeline, "llm", None)
                slog.log_turn(
                    user=transcript,
                    assistant=result.assistant_text or "",
                    stt_engine=getattr(self.pipeline, "stt_engine", None),
                    llm_backend=getattr(self.pipeline, "llm_backend", None),
                    llm_model=getattr(llm, "hef_path", None) or getattr(llm, "model", None),
                    tool_name=result.tool_name,
                    metrics=metrics,
                )
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
            self.pipeline.on_tool_status = None
            self.pipeline.on_token = None
            self.pvad.reset()
