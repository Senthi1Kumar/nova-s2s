"""One OpenAI-realtime-shaped WebSocket session over NovaPipeline streaming cascade.

Callers: nova_hailo/web/app.py websocket /v1/realtime.
WS event schema: {type, audio|delta|transcript|response|bench|generation_id}.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
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
from nova_hailo.web import settings_ops
from nova_hailo.cabingym.events import vehicle_delta_event, vehicle_snapshot_event
from nova_hailo.web.cards import delivery_card_events, turn_card
from nova_hailo.web.observe import HUB as OBSERVERS
from nova_hailo.cabingym.confirm import parse_confirmation
from nova_hailo.web.echo_guard import EchoGuard, fold_words
from nova_hailo.web.session_control import CONTROL
from nova_hailo.web.research_speech import delivery_trace_events, research_announcement
from nova_hailo.web.settings_ops import (
    google_connect_payload as _google_connect_payload,
    google_disconnect_payload as _google_disconnect_payload,
    integrations_snapshot as _integrations_snapshot,
)
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
OVERLAP_STALE_S = 0.35
BARGE_IN_WAIT_S = 2.0
DEFAULT_REOPEN_MS = 300
# HMI already debounce-holds 1.8 s; keep the server echo gate across chunk gaps.
PLAYBACK_STOPPED_HOLD_S = 1.8
PROTOCOL_VERSION = "nova-realtime/1"
OUTPUT_SAMPLE_RATES = (16000, 22050, 24000, 48000)
# One TTS chunk is a whole sentence; sent as one delta it overflows the receive
# buffer of a microcontroller client. Short deltas also let a device start
# playing after one slice instead of one sentence.
MAX_DELTA_MS = 200
# Longest utterance that holds the follow-up window open; past this the VAD is
# assumed to have lost the end of speech, and the window may expire again.
MAX_UTTERANCE_S = 30.0


def apply_playback_stopped(
    *,
    now: float,
    echo_until: float,
    echo_tail_s: float,
    hold_s: float = PLAYBACK_STOPPED_HOLD_S,
) -> dict[str, float | bool]:
    """Chunk-gap stopped must not reopen the mic."""
    return {
        "client_playing": False,
        "release_speaking": False,
        "echo_until": max(float(echo_until), float(now) + float(echo_tail_s) + float(hold_s)),
    }


def apply_playback_interrupted() -> dict[str, float | bool]:
    """Barge-in / cancel: drop the echo gate immediately."""
    return {"client_playing": False, "release_speaking": True, "echo_until": 0.0}


def _fold_echo_text(s: str) -> str:
    t = (s or "").lower().replace("'", "")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\d+", "#", t)
    return " ".join(t.split())


# Share of the transcript found, in order, inside Nova's line. A real echo is a
# garbled stretch of that line (0.8 and up); a follow-up that only reuses its
# words ("switch the drive mode to sport" after "Switch to sport mode?") stays
# under 0.6.
ECHO_COVERAGE = 0.75


def _echo_coverage(user: str, prev: str) -> float:
    if not user:
        return 0.0
    blocks = SequenceMatcher(None, user, prev, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 3) / len(user)


def _transcript_is_echo(user: str, last_assistant: str) -> bool:
    """True when ASR likely captured Nova's own last speak-back.

    Only meaningful for speech that began while Nova was audible or just after
    (the caller's echo_suspect); later speech is the user, whatever it says."""
    u = (user or "").strip().lower()
    prev = (last_assistant or "").strip().lower()
    if not prev or len(u) < 4:
        return False
    if u in prev or prev[:24] in u:
        return True
    pw = {w for w in prev.replace("'", "").split() if len(w) > 2}
    uw = {w for w in u.replace("'", "").split() if len(w) > 2}
    if uw:
        overlap = len(pw & uw) / max(1, len(uw))
        # Word overlap ignores order, so it only marks a short garbled
        # fragment as echo ("It's Tense." for "It's 10:09 am."). A full
        # follow-up reuses the last answer's topic words ("the current stock
        # price") by nature; a real long echo is caught by the ordered checks.
        if overlap >= 0.45 and len(uw) <= 4:
            return True
        uf = _fold_echo_text(u).split()
        pf = _fold_echo_text(prev).split()
        if uf and pf and uf[0] == pf[0] and len(uf) <= 4 and overlap >= 0.30:
            return True
    uf = _fold_echo_text(u)
    pf = _fold_echo_text(prev)
    return bool(uf and pf and _echo_coverage(uf, pf) >= ECHO_COVERAGE)


def should_interrupt_on_user_speech(
    *,
    fsm_state: SessionState,
    tts_speaking: bool,
    barge_in_while_speaking: bool,
    client_playing: bool = False,
) -> bool:
    """True only when Nova is audibly speaking and barge-in is enabled.

    Speech during TRANSCRIBING/THINKING is a repeat while the first turn is
    still unanswered. Aborting that turn made the first utterance look dropped.
    ``client_playing`` covers HMI/browser playback after server synthesis ends.
    """
    if fsm_state == SessionState.INTERRUPTING:
        return False
    audible = (
        bool(tts_speaking)
        or bool(client_playing)
        or fsm_state == SessionState.SPEAKING
    )
    return bool(audible and barge_in_while_speaking)


# Speaking a finished result is safe in any of these. LISTENING matters most:
# every turn ends with arm() *followed by*
# begin_listen(), so LISTENING -- mic open, nothing captured yet -- is the
# resting state, not ARMED. Omitting it made the guard below reject every tick,
# so a finished result was never spoken at all. Talking over the user is still
# prevented by tts.is_speaking, the turn lock and abort_event; the states left
# out (TRANSCRIBING/THINKING/SPEAKING/INTERRUPTING) are the ones where a turn
# is genuinely mid-flight.
_RESEARCH_QUIET_STATES = frozenset(
    {SessionState.IDLE, SessionState.ARMED, SessionState.LISTENING}
)


def _pcm16_b64(audio: np.ndarray) -> str:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    i16 = (x * 32767.0).astype(np.int16)
    return base64.b64encode(i16.tobytes()).decode("ascii")


def _b64_pcm16(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii") if isinstance(data, str) else data)


def _resample_f32(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Per-chunk polyphase resample for devices whose codec runs at one rate.

    Chunks are whole TTS clauses, so boundaries fall in speech pauses where the
    filter's edge transient is inaudible; a stateful resampler is not worth a
    new dependency for that.
    """
    if src == dst:
        return x
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(int(src), int(dst))
    y = resample_poly(np.asarray(x, dtype=np.float32).reshape(-1), dst // g, src // g)
    return y.astype(np.float32, copy=False)


@dataclass
class PendingUtterance:
    audio: np.ndarray
    nemo_result: dict | None
    t0: float
    kind: str = "overlap"  # barge_in | overlap
    speech_stopped_at: float | None = None
    turn_revision: int = 0
    echo_suspect: bool = False


def _concat_audio(left: np.ndarray | None, right: np.ndarray) -> np.ndarray:
    right_a = np.asarray(right, dtype=np.float32).reshape(-1)
    if left is None:
        return right_a
    return np.concatenate([np.asarray(left, dtype=np.float32).reshape(-1), right_a])


class RealtimeSession:
    """Generation-safe realtime session with explicit FSM + bounded pending queue."""

    # Mic mute, display arm and × cancel (web.session_control); the mute is the cabin's.
    control = CONTROL
    # Echo defences (config voice.*); class values are the defaults.
    trust_client_aec = False
    echo_suspect_s = 1.5
    fail_closed_cooldown_s = 6.0
    dedupe_user_s = 8.0
    _last_audible_mono = 0.0
    _last_barge_mono = 0.0
    _utterance_started_mono: float | None = None
    _last_user_turn: tuple[str, float] | None = None
    # What Nova is (or was last) audibly saying: "answer" or "guidance". Route
    # prompts do not close the mic, and the user can talk over them.
    _audible_kind = "answer"

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
        # Results drained from the pipeline but not yet spoken. drain_* removes
        # items, so anything left unspoken when the user starts talking would
        # otherwise be lost; it waits here for the next quiet tick instead.
        self._research_backlog: list[dict[str, Any]] = []
        self._closed = False
        self._response_id: str | None = None
        self._pending: collections.deque[PendingUtterance] = collections.deque(maxlen=4)
        self._nemo_ws = None
        self._nemo_ws_task: asyncio.Task | None = None
        self._research_task: asyncio.Task | None = None
        self._reopen_task: asyncio.Task | None = None
        self._nemo_stream_result: dict | None = None
        self._utterance_audio: np.ndarray | None = None
        self._turn_revision = 0
        self._ptt_armed = False
        self._logged_ignore_auto_arm = False
        wake_mode_env = (os.environ.get("NOVA_HAILO_WAKE_MODE") or "").strip().lower()
        if wake_mode_env in {"kws", "ptt"}:
            wake_mode = wake_mode_env
        else:
            wake_mode = str(cfg.get("wake", "mode", default="ptt") or "ptt").lower()
        self.wake = WakeWordDetector.from_config(cfg)
        self.gate_on_wake = wake_mode == "kws" and self.wake.available
        if hasattr(self.fsm, "_expire_listening"):
            self.fsm._expire_listening = self.gate_on_wake
        self.require_arm = self.gate_on_wake
        if wake_mode == "ptt" and not self.wake.available:
            self.require_arm = True
        # Where the wake gate lives for this session. "server": nova_pro_v3.onnx
        # scores the uplink. "device": the client ran its own wake word and says
        # so with session.arm. "none": no gate (PTT profile without KWS).
        self._wake_source = "server" if self.gate_on_wake else "none"
        # None = client did not declare; keep the HMI-tuned echo behavior.
        self._client_aec: bool | None = None
        # None = send TTS at its native rate (reported on every delta).
        self._output_rate: int | None = None
        self.pvad = OptionalFireRedPVAD(
            enabled=bool(cfg.get("pvad", "enabled", default=False)),
            model_dir=cfg.get("pvad", "model_dir"),
            threshold=float(cfg.get("pvad", "barge_in_threshold", default=0.55) or 0.55),
            required_frames=int(cfg.get("pvad", "barge_in_frames", default=2) or 2),
        )
        cfgv = pipeline.cfg
        barge_env = (os.environ.get("NOVA_HAILO_BARGE_IN_WHILE_SPEAKING") or "").strip().lower()
        if barge_env:
            self.barge_in_while_speaking = barge_env in {"1", "true", "yes", "on"}
        else:
            self.barge_in_while_speaking = bool(
                cfgv.get("voice", "barge_in_while_speaking", default=False)
            )
        if self.gate_on_wake:
            wake_banner = (
                f"kws model={self.wake.model_path or '?'} "
                f"thr={self.wake.threshold}"
            )
        else:
            wake_banner = "ptt"
        print(
            f"[voice] barge_in_while_speaking={self.barge_in_while_speaking} "
            f"echo_tail_ms={cfgv.get('voice', 'echo_tail_ms', default=400)} "
            f"stt={getattr(pipeline, 'stt_engine', None)} "
            f"wake={wake_banner}",
            flush=True,
        )
        self.echo_tail_ms = int(cfgv.get("voice", "echo_tail_ms", default=400) or 400)
        trust_env = (os.environ.get("NOVA_HAILO_TRUST_CLIENT_AEC") or "").strip().lower()
        self.trust_client_aec = (
            trust_env in {"1", "true", "yes", "on"} if trust_env
            else bool(cfgv.get("voice", "trust_client_aec", default=False))
        )
        self.echo_suspect_s = float(cfgv.get("voice", "echo_suspect_s", default=1.5) or 0.0)
        self.fail_closed_cooldown_s = float(
            cfgv.get("voice", "fail_closed_cooldown_s", default=6.0) or 0.0)
        self.dedupe_user_s = float(cfgv.get("voice", "dedupe_user_s", default=8.0) or 0.0)
        self.echo_guard = EchoGuard()
        self._echo_until = 0.0
        self._echo_logged = False
        self._client_playing = False
        self.echo_max_ms = int(cfgv.get("voice", "echo_max_ms", default=12000) or 12000)
        # Energy gate knobs (audio_gate.accept_utterance); defaults match module constants.
        self.gate_min_rms = float(
            os.environ.get("NOVA_HAILO_GATE_MIN_RMS")
            or cfgv.get("voice", "gate_min_rms", default=DEFAULT_MIN_RMS)
            or DEFAULT_MIN_RMS
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
        self.reopen_ms = int(
            cfgv.get("voice", "reopen_ms", default=DEFAULT_REOPEN_MS) or DEFAULT_REOPEN_MS
        )
        self._playback_stop_ack_at: float | None = None
        self._first_audio_ack_at: float | None = None
        self._last_fail_closed_mono: float = float("-inf")
        self._echo_hold_started: float = 0.0
        self._uplink_frames: int = 0
        self._uplink_log_at: float = 0.0
        self._wake_peak: float = 0.0

    async def send(self, event: dict[str, Any], mirror: bool = True):
        if self._closed or self.ws.client_state != WebSocketState.CONNECTED:
            return
        if mirror:
            OBSERVERS.publish_session_event(event)  # displays on /v1/observe see it too
        try:
            await self.ws.send_text(json.dumps(event))
        except Exception:
            pass

    def _on_vehicle_changes(self, changes) -> None:
        """VssStore listener; may run on a worker thread, so hop to the loop."""
        self._vehicle_seq += 1
        self._schedule_send(vehicle_delta_event(changes, self._vehicle_seq))

    def _schedule_send(self, event: dict[str, Any], mirror: bool = True):
        if self._closed:
            return
        coro = self.send(event) if mirror else self.send(event, mirror=False)
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- mute / arm / cancel from displays (web.session_control) -------------

    def control_mute(self, muted: bool) -> None:
        self._call_on_loop(self._apply_mute, bool(muted))

    def control_arm(self) -> None:
        self._call_on_loop(self._on_wake_hit, "cockpit")

    def control_cancel(self) -> None:
        self._call_on_loop(self._cancel_from_control)

    def control_state(self) -> str:
        return self.fsm.state.value

    def _call_on_loop(self, fn: Any, *args: Any) -> None:
        loop = getattr(self, "loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop or not loop.is_running():
            fn(*args)
        else:
            loop.call_soon_threadsafe(fn, *args)

    def _apply_mute(self, muted: bool) -> None:
        """Mic off (or on). What Nova is saying finishes; what the user was half-way
        through saying is dropped, so unmuting starts clean."""
        if muted:
            self._speech_started_mono = None
            if hasattr(self.vad, "reset"):
                self.vad.reset()
            self._utterance_audio = None
            self._turn_revision = 0
            self._cancel_reopen()
            loop = getattr(self, "loop", None)
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._abort_nemo_stream(), loop)
        print(f"[mic] {'muted' if muted else 'unmuted'} by {self.control.source}", flush=True)
        # Only this session's client: the control already told /v1/observe.
        self._schedule_send({"type": "nova.mic", "muted": muted, "source": self.control.source},
                            mirror=False)

    def _cancel_from_control(self) -> None:
        self._barge_in(reason="ui_cancel")
        self.fsm.force_idle()

    async def _send_error(self, err_type: str, message: str) -> None:
        # Protocol errors keep the socket open: a device with one bad frame
        # should not have to redo TLS + auth to recover.
        await self.send({"type": "error", "error": {"type": err_type, "message": message}})

    def _audio_delta_events(
        self, chunk: np.ndarray, sample_rate: int, generation_id
    ) -> list[dict[str, Any]]:
        rate = int(sample_rate)
        out_rate = getattr(self, "_output_rate", None) or rate
        x = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if out_rate != rate:
            x = _resample_f32(x, rate, out_rate)
        step = max(1, out_rate * MAX_DELTA_MS // 1000)
        return [
            {
                "type": "response.audio.delta",
                "delta": _pcm16_b64(x[i : i + step]),
                "sample_rate": out_rate,
                "generation_id": generation_id,
            }
            for i in range(0, max(x.size, 1), step)
        ]

    def _send_audio(self, chunk: np.ndarray, sample_rate: int, generation_id) -> None:
        for event in self._audio_delta_events(chunk, sample_rate, generation_id):
            self._schedule_send(event)

    def _session_created_payload(self, greeting: str) -> dict[str, Any]:
        return {
            "type": "session.created",
            "session": {
                "id": self.session_id,
                "type": "realtime",
                "model": "nova-hailo",
                "protocol": PROTOCOL_VERSION,
                "audio": {
                    "input": {"format": "pcm16", "sample_rate": 16000, "channels": 1},
                    # null = native TTS rate, stated on every delta; devices pin
                    # it with session.update audio.output.sample_rate.
                    "output": {"format": "pcm16", "sample_rate": self._output_rate, "channels": 1},
                },
                "wake_source": self._wake_source,
                "wake": self.wake.status(),
                "pvad": self.pvad.status(),
                "fsm": self.fsm.snapshot(),
                "stt_engine": getattr(self.pipeline, "stt_engine", None),
                "greeting": greeting,
                "llm": getattr(self.pipeline, "llm_settings_snapshot", lambda: {})(),
            },
        }

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
        snap = dict(settings_ops.llm_snapshot(self.pipeline))
        snap.update(self._voice_snapshot())
        snap.update(_integrations_snapshot())
        # The provider/model shape displays read (GET /v1/settings), next to the HMI's flat keys.
        snap["llm"] = settings_ops.llm_settings(self.pipeline)
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
        snap, status = await settings_ops.apply_llm_settings(self.pipeline, payload)
        if snap is not None:
            await self.send(
                {
                    "type": "nova.settings",
                    **{**snap, **self._voice_snapshot(), **_integrations_snapshot()},
                }
            )
        await self.send(status)

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
        if (
            not self._research_backlog
            and not self.pipeline.pending_research()
            and not self._nav_guidance_waiting()
        ):
            return  # cheap peek; avoids a thread spawn on every idle tick

        def blocked(why: str) -> None:
            """Name the guard that withheld a ready result, once per reason.

            A finished job that is never spoken looks exactly like one that
            never finished, so the reason has to reach the log.
            """
            if why != getattr(self, "_research_block_last", None):
                self._research_block_last = why
                n = len(self.pipeline.pending_research()) + len(self._research_backlog)
                print(f"[research] holding {n} result(s): {why}", flush=True)

        def worker() -> None:
            if self._closed or self.pipeline.abort_event.is_set():
                return blocked("closed/abort")
            if not self._turn_lock.acquire(blocking=False):
                return blocked("turn in flight")  # never wait for a real turn
            try:
                if self._closed or self.pipeline.abort_event.is_set():
                    return blocked("closed/abort")
                if self.fsm.state not in _RESEARCH_QUIET_STATES:
                    return blocked(f"state={self.fsm.state.value}")
                if self.pipeline.tts.is_speaking:
                    return blocked("tts speaking")
                self._research_block_last = None
                # Backlog first, so an interrupted delivery resumes in order.
                queue = self._research_backlog + self.pipeline.drain_finished_research()
                self._research_backlog = []
                had_research = bool(queue)
                while queue:
                    if self._closed or self.pipeline.abort_event.is_set():
                        break
                    if self.fsm.state not in _RESEARCH_QUIET_STATES or self.pipeline.tts.is_speaking:
                        break
                    item = queue.pop(0)
                    ok = self._speak_research_result(item)
                    print(
                        f"[research] deliver job={str(item.get('job_id'))[:12]} spoken={ok}",
                        flush=True,
                    )
                    if not ok:
                        # Popped but not said: putting it back is the whole
                        # point of the backlog. Dropping it here lost the
                        # answer the user explicitly asked for.
                        queue.insert(0, item)
                        break
                # Whatever we could not say now is kept, not dropped: the user
                # asked for this research and a later quiet tick will deliver it.
                self._research_backlog = queue
                # A route prompt waits until a tick that has nothing else to say.
                if not had_research and not queue:
                    self._speak_nav_guidance()
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
        prefixed = research_announcement(item)
        if not prefixed:
            return False
        self.pipeline.begin_turn()  # clear abort/barge-in/gen_guard before speaking
        my_gen = self.pipeline.gen_guard.generation_id
        for event in delivery_trace_events(item, my_gen) + delivery_card_events(item, my_gen):
            self._schedule_send(event)  # a delegated result's calls, shown as it is spoken

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
            self._audible_kind = "answer"
            self._send_audio(chunk, sample_rate, my_gen)

        def on_clause(text: str) -> None:
            self._guard().spoken(text)
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
            # Same set the caller gates on. Demanding IDLE here contradicted
            # that outer guard: every turn ends parked in LISTENING, so this
            # rejected every delivery the caller had just approved, and the
            # result was popped off the queue and silently destroyed.
            if (
                self.fsm.state not in _RESEARCH_QUIET_STATES
                or self.pipeline.abort_event.is_set()
            ):
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
            self.pipeline.on_agent_trace = None
            self.pipeline.on_token = None

    def _nav_guidance_waiting(self) -> bool:
        feed = getattr(self.pipeline, "nav_feed", None)
        waiting = getattr(feed, "guidance_waiting", None)
        return bool(waiting and waiting())

    def _speak_nav_guidance(self) -> bool:
        """Speak one route prompt. Mute does not stop this; a live turn does."""
        feed = getattr(self.pipeline, "nav_feed", None)
        take = getattr(feed, "take_guidance", None)
        if take is None or not self._nav_guidance_waiting():
            return False
        if (
            self.fsm.state not in _RESEARCH_QUIET_STATES
            or self.pipeline.abort_event.is_set()
            or self.pipeline.tts.is_speaking
        ):
            return False
        line = take()
        if not line:
            return False
        print(f"[guidance] {line}", flush=True)
        self.pipeline.begin_turn()
        my_gen = self.pipeline.gen_guard.generation_id
        self._guidance_gen = my_gen

        def on_pcm(chunk: np.ndarray, sample_rate: int) -> None:
            if not self.pipeline.gen_guard.is_live(my_gen):
                return
            dur = len(np.asarray(chunk).reshape(-1)) / float(max(sample_rate, 1))
            now_m = time.monotonic()
            self._echo_until = min(
                max(self._echo_until, now_m) + dur, now_m + self.echo_max_ms / 1000.0
            )
            self._audible_kind = "guidance"
            self._send_audio(chunk, sample_rate, my_gen)

        def on_clause(text: str) -> None:
            self._guard().spoken(text)
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
            if (
                self.fsm.state not in _RESEARCH_QUIET_STATES
                or self.pipeline.abort_event.is_set()
            ):
                return False
            tm = TurnMetrics(generation_id=my_gen, notes=["nav_guidance"])
            result = self.pipeline._speak_tool_line(
                line,
                wall0=time.perf_counter(),
                metrics=tm,
                user_text="",
                auth_decision="n/a",
                tool_name="navigation",
                record_session=False,
                # A route prompt is not conversation: in history it pushed real
                # turns out of the window and became the echo filter's reference.
                add_history=False,
            )
            if not self.pipeline.gen_guard.is_live(my_gen):
                return False
            self._schedule_send(
                {
                    "type": "response.audio_transcript.done",
                    "transcript": line,
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
                        "metadata": {"nav_guidance": True, "generation_id": my_gen},
                    },
                }
            )
            return True
        finally:
            self.pipeline.on_audio_pcm = None
            self.pipeline.on_validated_clause = None
            self.pipeline.on_research_status = None
            self.pipeline.on_tool_status = None
            self.pipeline.on_agent_trace = None
            self.pipeline.on_token = None

    async def _research_tick(self) -> None:
        """Poll for finished async research instead of waiting on an IDLE edge.

        Every turn ends parked in LISTENING, and a quiet user produces no
        further FSM transition at all, so hanging delivery off an IDLE edge
        meant a finished job was never spoken. This ticks regardless;
        ``_maybe_deliver_research`` still does all the never-talk-over-the-user
        gating and re-queues anything it could not say yet.
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
        # One WebSocket is one conversation (docs/api/realtime.md). The pipeline
        # outlives connections, so drop the last one's history: its last reply is
        # the echo filter's reference, and its unanswered question would be
        # replayed on this connection's first turn. Long-term memory stays.
        clear = getattr(self.pipeline, "clear_context", None)
        if clear is not None:
            clear()
        if not self.gate_on_wake:
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
        await self.send(self._session_created_payload(greeting))
        self._vehicle_seq = 0
        self._vehicle_unsub = None
        cabin = getattr(getattr(self.pipeline, "oem_gateway", None), "cabin", None)
        if cabin is not None:
            await self.send(vehicle_snapshot_event(cabin))
            self._vehicle_unsub = cabin.store.subscribe(self._on_vehicle_changes)
        if getattr(self.pipeline, "llm_settings_snapshot", None) is not None:
            await self.send({"type": "nova.settings", **self._settings_payload()})
        await self.send({"type": "nova.mic", "muted": self.control.muted,
                         "source": self.control.source}, mirror=False)
        self._control_unregister = self.control.register(self)
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
                message = await self.ws.receive()
                if not await self._dispatch_frame(message):
                    break
        except WebSocketDisconnect:
            pass
        finally:
            self._closed = True
            unregister = getattr(self, "_control_unregister", None)
            if unregister is not None:
                unregister()
            unsub = getattr(self, "_vehicle_unsub", None)
            if unsub is not None:
                unsub()
                self._vehicle_unsub = None
            task = self._research_task
            self._research_task = None
            if task is not None:
                task.cancel()
            self.pipeline.on_abort()
            self._cancel_reopen()
            await self._finish_nemo_stream()

    def _release_speaking_if_quiet(self) -> None:
        if getattr(self, "_closed", False):
            return
        if (
            self.fsm.state == SessionState.SPEAKING
            and not self.pipeline.tts.is_speaking
            and not self._client_playing
            and time.monotonic() >= float(getattr(self, "_echo_until", 0.0) or 0.0)
        ):
            self.fsm.release_speaking()

    def _schedule_speaking_release(self) -> None:
        # Leaving SPEAKING used to wait for the next uplink frame, but a device
        # without AEC may hold its mic until the server leaves SPEAKING --
        # neither side would move. Check again once the echo hold has passed.
        loop = getattr(self, "loop", None)
        if loop is None:
            return
        delay = max(0.0, float(getattr(self, "_echo_until", 0.0) or 0.0) - time.monotonic())
        loop.call_later(delay + 0.05, self._release_speaking_if_quiet)

    def _barge_allowed(self) -> bool:
        # A client that says it has no echo canceller always closes the gate. One
        # that claims a canceller is believed only with voice.trust_client_aec:
        # a laptop "AEC" that did not cancel let Nova's voice fire tools and turns.
        aec = getattr(self, "_client_aec", None)
        if aec is True and not getattr(self, "trust_client_aec", False):
            aec = None
        if aec is None:
            return bool(self.barge_in_while_speaking)
        return bool(aec)

    def _set_wake_source(self, source: str) -> None:
        self._wake_source = source
        # Both declared modes gate turns on a wake event and let the follow-up
        # window expire to IDLE, which is what tells a device to stop streaming.
        self.gate_on_wake = True
        self.require_arm = True
        if hasattr(self.fsm, "_expire_listening"):
            self.fsm._expire_listening = True
        if source == "device":
            # run() parks non-KWS profiles in LISTENING at connect; a device
            # that owns the wake word expects IDLE until its session.arm.
            self.fsm._armed_until = 0.0
            self.fsm.maybe_expire_arm()

    async def _apply_capabilities(self, session: Any) -> bool:
        """Validate and apply device capabilities; False = rejected with an error."""
        inp = session.get("input") if isinstance(session, dict) else None
        audio = session.get("audio") if isinstance(session, dict) else None
        out = audio.get("output") if isinstance(audio, dict) else None
        if (
            not isinstance(session, dict)
            or not isinstance(inp or {}, dict)
            or not isinstance(audio or {}, dict)
            or not isinstance(out or {}, dict)
        ):
            await self._send_error(
                "invalid_session_update", "session, session.input and session.audio.output must be objects"
            )
            return False
        inp = inp or {}
        out = out or {}
        muted = inp.get("muted")
        if muted is not None and not isinstance(muted, bool):
            await self._send_error("invalid_session_update", "input.muted must be true or false")
            return
        wake = inp.get("wake")
        if wake is not None:
            if wake not in ("server", "device"):
                await self._send_error(
                    "invalid_session_update",
                    f"input.wake must be 'server' or 'device', got {wake!r}",
                )
                return
            if wake == "server" and not self.wake.available:
                await self._send_error(
                    "invalid_session_update",
                    "server wake word is not available on this Pi; "
                    "use input.wake='device' and send session.arm",
                )
                return
        aec = inp.get("echo_cancellation")
        if aec is not None and not isinstance(aec, bool):
            await self._send_error(
                "invalid_session_update", "input.echo_cancellation must be true or false"
            )
            return
        rate = out.get("sample_rate")
        if rate is not None and (
            isinstance(rate, bool) or not isinstance(rate, int) or rate not in OUTPUT_SAMPLE_RATES
        ):
            await self._send_error(
                "unsupported_sample_rate",
                f"audio.output.sample_rate must be one of {list(OUTPUT_SAMPLE_RATES)}",
            )
            return
        if wake is not None:
            self._set_wake_source(wake)
        if aec is not None:
            self._client_aec = aec
        if rate is not None:
            self._output_rate = int(rate)
        if muted is not None:
            self.control.set_muted(muted, "client")
        return True

    async def _dispatch_frame(self, message: dict[str, Any]) -> bool:
        """Route one ASGI websocket message; False means the peer went away.

        Binary frames are raw PCM16LE 16 kHz mono -- the always-on uplink from
        small devices, which should not pay base64 (+33% bytes, CPU) forever.
        Text frames are the JSON events.
        """
        if message.get("type") == "websocket.disconnect":
            return False
        data = message.get("bytes")
        if data is not None:
            if len(data) % 2:
                await self._send_error(
                    "invalid_audio_frame",
                    f"binary frames are PCM16LE; got {len(data)} bytes (odd)",
                )
                return True
            if data:
                await self._on_uplink_pcm(data)
            return True
        raw = message.get("text")
        if raw is None:
            return True
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return True
        if isinstance(msg, dict):
            await self._handle_client(msg)
        return True

    async def _handle_client(self, msg: dict[str, Any]):
        mtype = msg.get("type")
        if mtype == "session.update":
            session = msg.get("session") or {}
            if not await self._apply_capabilities(session):
                return
            audio = session.get("audio") or {}
            inp = audio.get("input") or {}
            td = inp.get("turn_detection") or {}
            thr = td.get("threshold")
            if thr is not None:
                self.vad.set_threshold(float(thr))
            if session.get("arm") or session.get("ptt"):
                if self.control.muted:
                    await self._send_error("mic_muted", "the microphone is muted")
                    return
                if self.gate_on_wake:
                    if not self._logged_ignore_auto_arm:
                        print(
                            "[wake] ignoring HMI auto-arm (gated; say Nova or use PTT)",
                            flush=True,
                        )
                        self._logged_ignore_auto_arm = True
                    return
                self._ptt_armed = True
                self.fsm.arm()
                self.fsm.begin_listen()
                await self.send({"type": "session.armed", "fsm": self.fsm.snapshot()})
            return

        if mtype == "nova.settings.get":
            await self.send({"type": "nova.settings", **self._settings_payload()})
            return

        if mtype == "nova.settings.set":
            payload = settings_ops.settings_body(msg)
            voice_only = not settings_ops.changes_llm(payload)
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
            if self.control.muted:
                await self._send_error("mic_muted", "the microphone is muted")
                return
            self._ptt_armed = True
            if self._wake_source == "device":
                # The device's wake word is this session's wake event: same
                # path as a server KWS hit, including barge-in over playback.
                self._on_wake_hit(source="device")
                return
            self.fsm.arm()
            self.fsm.begin_listen()
            await self.send({"type": "session.armed", "fsm": self.fsm.snapshot()})
            return

        if mtype == "playback.started":
            self._first_audio_ack_at = time.perf_counter()
            self._client_playing = True
            print("[play] started", flush=True)
            ctx = self.fsm.current
            if ctx is not None and ctx.first_audio_played_at is None:
                ctx.first_audio_played_at = self._first_audio_ack_at
            return

        if mtype == "playback.interrupted":
            self._playback_stop_ack_at = time.perf_counter()
            decision = apply_playback_interrupted()
            self._client_playing = bool(decision["client_playing"])
            self._echo_until = float(decision["echo_until"])
            print("[play] playback.interrupted", flush=True)
            ctx = self.fsm.current
            if ctx is not None and ctx.barge_in_at is not None:
                stop_ms = (self._playback_stop_ack_at - ctx.barge_in_at) * 1000
                ctx.metadata["barge_in_stop_ms"] = stop_ms
            return

        if mtype == "playback.stopped":
            self._playback_stop_ack_at = time.perf_counter()
            decision = apply_playback_stopped(
                now=time.monotonic(),
                echo_until=float(getattr(self, "_echo_until", 0.0) or 0.0),
                echo_tail_s=self.echo_tail_ms / 1000.0,
            )
            self._client_playing = bool(decision["client_playing"])
            self._echo_until = float(decision["echo_until"])
            self._last_audible_mono = time.monotonic()
            print("[play] playback.stopped", flush=True)
            self._schedule_speaking_release()
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
                not self._barge_allowed()
                and time.monotonic()
                < getattr(self, "_echo_until", 0.0) + self.echo_tail_ms / 1000.0
            ):
                return
            if should_interrupt_on_user_speech(
                fsm_state=self.fsm.state,
                tts_speaking=bool(self.pipeline.tts.is_speaking),
                barge_in_while_speaking=self._barge_allowed(),
                client_playing=bool(self._client_playing),
            ):
                self._barge_in(reason="client_cancel")
            return

        if mtype == "input_audio_buffer.append":
            audio_b64 = msg.get("audio")
            if not audio_b64:
                return
            await self._on_uplink_pcm(_b64_pcm16(audio_b64))
            return

        if mtype == "input_audio_buffer.commit":
            return

    def _speech_in_progress(self) -> bool:
        started = getattr(self, "_speech_started_mono", None)
        return started is not None and time.monotonic() - started < MAX_UTTERANCE_S

    async def _on_uplink_pcm(self, pcm: bytes) -> None:
        """One chunk of mic PCM16 from any transport (JSON append or binary frame)."""
        if self.control.muted:
            return  # mic off: nothing reaches the wake word, the VAD or STT
        self._uplink_frames += 1
        now_m = time.monotonic()
        # Never expire the follow-up window under a user who is mid-sentence:
        # when frames arrive late (a stalled loop, a slow link) the deadline
        # passes during speech, and IDLE would silently drop the question.
        if not self._speech_in_progress():
            self.fsm.maybe_expire_arm()

        audible = (
            self.fsm.state == SessionState.SPEAKING
            or bool(self.pipeline.tts.is_speaking)
            or bool(getattr(self, "_client_playing", False))
        )
        if audible:
            self._last_audible_mono = now_m
        wake_thr = self.wake.barge_threshold if audible else None
        # No server scoring when the device owns the wake word, and none while
        # a no-AEC device plays Nova's voice ("...ask Nova..." would self-wake).
        score_wake = (
            self.wake.enabled
            and getattr(self, "_wake_source", "server") != "device"
            and not (audible and getattr(self, "_client_aec", None) is False)
        )
        if score_wake and self.wake.process(pcm, threshold=wake_thr, ignore_cooldown=audible):
            self._on_wake_hit()
            return

        score = float(getattr(self.wake, "last_score", 0.0) or 0.0)
        self._wake_peak = max(getattr(self, "_wake_peak", 0.0), score)
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
                f"speaking={self.pipeline.tts.is_speaking} "
                f"playing={int(bool(self._client_playing))} "
                f"wake={score:.3f} peak={self._wake_peak:.3f}",
                flush=True,
            )
            self._wake_peak = 0.0

        if self.gate_on_wake and self.fsm.state == SessionState.IDLE:
            self.vad.feed(pcm)
            return

        self._release_speaking_if_quiet()
        speaking = (
            self.fsm.state == SessionState.SPEAKING
            or self.pipeline.tts.is_speaking
            or self._client_playing
        )
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
        gated = (
            (speaking or in_echo_window)
            and not self._barge_allowed()
            and self._audible_kind != "guidance"
        )
        if (
            gated
            and echo_held_s > echo_cap
            and not self._client_playing
            and time.monotonic() >= float(getattr(self, "_echo_until", 0.0) or 0.0)
        ):
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
        for ev, _payload in vad_events:
            if ev == "speech_started":
                self._speech_started_mono = time.monotonic()
            elif ev == "speech_stopped":
                self._speech_started_mono = None
        for ev, payload in vad_events:
            if ev == "speech_started":
                print(
                    f"[vad] speech_started fsm={self.fsm.state.value} "
                    f"tts={self.pipeline.tts.is_speaking}",
                    flush=True,
                )
                await self.send({"type": "input_audio_buffer.speech_started"})
                reopen_pending = self._reopen_task is not None
                if reopen_pending:
                    print("[vad] reopen cancelled (speech again)", flush=True)
                    self._cancel_reopen()
                    self._turn_revision += 1
                    await self._start_nemo_stream(reuse=True)
                else:
                    self._utterance_started_mono = time.monotonic()
                    await self._start_nemo_stream(reuse=False)
                    if should_interrupt_on_user_speech(
                        fsm_state=self.fsm.state,
                        tts_speaking=speaking,
                        barge_in_while_speaking=self._barge_allowed(),
                        client_playing=bool(self._client_playing),
                    ):
                        self._barge_in(reason="vad_speech_started", abort_nemo=False)
                        # The utterance the segmenter is mid-way through
                        # is the one that just interrupted playback, so it
                        # must not be discarded for being short -- "stop"
                        # is the shortest thing a user ever says and the
                        # one they most expect to land.
                        self.vad.mark_barge_in()
                        self._utterance_audio = None
                        self._turn_revision = 0
                    else:
                        self.fsm.begin_listen()

        await self._nemo_send_pcm(pcm)

        for ev, payload in vad_events:
            if ev == "speech_stopped" and payload is not None:
                print(
                    f"[vad] speech_stopped sec={float(payload.size) / 16000.0:.2f} "
                    f"fsm={self.fsm.state.value}",
                    flush=True,
                )
                stopped_at = self.fsm.note_speech_stopped()
                await self.send({"type": "input_audio_buffer.speech_stopped"})
                self._utterance_audio = _concat_audio(self._utterance_audio, payload)
                audio_sec = float(self._utterance_audio.size) / 16000.0
                await self._schedule_utterance_commit(
                    self._utterance_audio,
                    audio_sec=audio_sec,
                    speech_stopped_at=stopped_at,
                )
                if self.ns is not None:
                    self.ns.reset()
        return

    def _on_wake_hit(self, source: str = "server") -> None:
        self._speech_started_mono = None  # the VAD is reset below
        score = float(getattr(self.wake, "last_score", 0.0) or 0.0) if source == "server" else None
        if hasattr(self.vad, "reset"):
            self.vad.reset()
        self._utterance_audio = None
        self._cancel_reopen()
        audible = (
            self.fsm.state == SessionState.SPEAKING
            or bool(self.pipeline.tts.is_speaking)
            or bool(getattr(self, "_client_playing", False))
        )
        if audible:
            self._barge_in(reason="wake_word")
        self.fsm.arm()
        self.fsm.begin_listen()
        print(
            f"[wake] hit source={source} score={score} fsm={self.fsm.state.value} barge={audible}",
            flush=True,
        )
        self._schedule_send(
            {"type": "wake.detected", "source": source, "score": score, "fsm": self.fsm.snapshot()}
        )

    def _barge_in(self, reason: str, *, abort_nemo: bool = True):
        self._last_barge_mono = time.monotonic()
        t0 = time.perf_counter()
        ctx = self.fsm.interrupt(reason=reason)
        t_llm0 = time.perf_counter()
        self.pipeline.on_abort()
        llm_abort_ms = (time.perf_counter() - t_llm0) * 1000
        # tts.interrupt lives inside on_abort; record the combined abort as
        # llm_abort_ms and a second stamp for flush (same window, split for logs).
        tts_flush_ms = llm_abort_ms
        detect_ms = (t_llm0 - t0) * 1000
        if ctx is not None:
            ctx.metadata["barge_in_detect_ms"] = detect_ms
            ctx.metadata["llm_abort_ms"] = llm_abort_ms
            ctx.metadata["tts_flush_ms"] = tts_flush_ms
        # Playback is being dropped, so stop gating the mic immediately.
        self._echo_until = 0.0
        self._client_playing = False
        self._cancel_reopen()
        if abort_nemo:
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
                        "barge_in_detect_ms": detect_ms,
                        "llm_abort_ms": llm_abort_ms,
                        "tts_flush_ms": tts_flush_ms,
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

    def _guard(self) -> EchoGuard:
        guard = self.__dict__.get("echo_guard")
        if guard is None:
            guard = self.echo_guard = EchoGuard()
        return guard

    def _stop_guidance(self) -> None:
        """A real turn ends a route prompt still playing (begin_turn already
        stopped its synthesis; this stops what the client has buffered)."""
        if self._audible_kind != "guidance":
            return
        if self._client_playing or time.monotonic() < float(getattr(self, "_echo_until", 0.0) or 0.0):
            self._schedule_send(
                {
                    "type": "playback.cancel",
                    "generation_id": getattr(self, "_guidance_gen", 0),
                    "reason": "user_turn",
                }
            )
        self._client_playing = False
        self._echo_until = 0.0
        self._audible_kind = "answer"

    def _confirmation_pending(self) -> bool:
        vehicle = getattr(getattr(self.pipeline, "oem_gateway", None), "vehicle", None)
        return getattr(vehicle, "pending", None) is not None

    def _skip_turn(self, generation_id: int, reason: str) -> None:
        """End a turn that must not reach the model (echo, repeat), without a word."""
        self.fsm.complete(generation_id, TurnTerminal.REJECTED, metadata={"skip": reason})
        self._schedule_send(
            {
                "type": "response.done",
                "response": {
                    "id": self._response_id,
                    "status": "completed",
                    "metadata": {"skipped": True, "reason": reason,
                                 "generation_id": generation_id},
                },
            }
        )

    def _echo_suspect(self, started: float | None) -> bool:
        """Speech that began while Nova was audible, within ``echo_suspect_s`` after,
        or right after a barge-in, may be her own voice: it gets no fail-closed reply
        and cannot seed the next prompt."""
        if started is None or self.echo_suspect_s <= 0:
            return False
        if self._last_audible_mono and started - self._last_audible_mono < self.echo_suspect_s:
            return True
        return 0.0 <= started - self._last_barge_mono < self.echo_suspect_s

    def _cancel_reopen(self) -> None:
        task = self._reopen_task
        self._reopen_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _schedule_utterance_commit(
        self,
        audio: np.ndarray,
        *,
        audio_sec: float,
        speech_stopped_at: float,
    ) -> None:
        """Hold STT commit across a short pause so mid-sentence cuts can reopen."""
        self._cancel_reopen()
        kind = (
            "barge_in"
            if self.fsm.state == SessionState.INTERRUPTING
            else "overlap"
        )
        delay_s = max(0.0, self.reopen_ms / 1000.0)
        echo_suspect = self._echo_suspect(self._utterance_started_mono)

        async def _commit():
            try:
                if delay_s:
                    await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                return
            self._reopen_task = None
            held = self._utterance_audio if self._utterance_audio is not None else audio
            self._utterance_audio = None
            revision = self._turn_revision
            self._turn_revision = 0
            sec = float(np.asarray(held).reshape(-1).size) / 16000.0
            nemo_result = await self._finish_nemo_stream(audio_sec=sec or audio_sec)
            self._start_turn(
                held,
                nemo_result=nemo_result,
                speech_stopped_at=speech_stopped_at,
                kind=kind,
                turn_revision=revision,
                echo_suspect=echo_suspect,
            )

        if delay_s <= 0:
            await _commit()
            return
        self._reopen_task = asyncio.create_task(_commit())

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

    async def _start_nemo_stream(self, reuse: bool = False):
        """Open a WebSocket to nemo-speech serve for the turn about to begin.

        C++ server does the decode (OpenAI-shaped /v1/realtime). Python only
        forwards PCM and waits for the final. No-op when stt_engine != nemo_speech
        or the sidecar is down (offline ctypes path in _run_turn_blocking).

        reuse=True keeps an already-open sidecar socket (mid-pause reopen).
        A new utterance must pass reuse=False so the *old* turn's stream is
        aborted and this stream belongs to the interrupter.
        """
        if reuse and self._nemo_ws is not None:
            return
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
                    partial = (result.get("partial") or "").strip()
                    if partial:
                        self._schedule_send(
                            {
                                "type": "conversation.item.input_audio_transcription.delta",
                                "delta": partial,
                                "transcript": partial,
                            }
                        )
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
        echo_suspect: bool = False,
        silent: bool = False,
    ) -> None:
        """Speak a fixed fallback when gate/STT fails — never an LLM. Silent for an
        echo suspect, within the cooldown, or when the caller says so (low energy).

        Callers send ``conversation.item.input_audio_transcription.completed``
        with an empty transcript first. Cooldown within FAIL_CLOSED_COOLDOWN_S
        skips TTS but still completes the turn with response.done.
        """
        print(f"[asr] fail_closed reason={reason}")
        now_m = time.monotonic()
        # Never for a suspect: the line itself echoes back and loops. Never twice
        # within the cooldown either.
        on_cooldown = silent or echo_suspect or not should_speak_fail_closed(
            self._last_fail_closed_mono, now_m, self.fail_closed_cooldown_s
        )
        silent_reason = (
            "echo_suspect" if echo_suspect else "low_energy" if silent else "fail_closed_cooldown"
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
            if silent_reason not in notes:
                notes.append(silent_reason)
            metrics["notes"] = notes
            metrics["reason"] = silent_reason
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
                            "reason": silent_reason,
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
            if not self._pcm_should_send(generation_id):
                return
            self.fsm.set_speaking(generation_id)
            self._client_playing = True
            if ctx is not None and ctx.first_pcm_sent_at is None:
                ctx.first_pcm_sent_at = time.perf_counter()
            # Extend echo guard for the duration of PCM we hand the browser.
            dur = len(np.asarray(chunk).reshape(-1)) / float(max(sample_rate, 1))
            now_echo = time.monotonic()
            self._echo_until = min(
                max(self._echo_until, now_echo) + dur,
                now_echo + self.echo_max_ms / 1000.0,
            )
            self._audible_kind = "answer"
            self._send_audio(chunk, sample_rate, generation_id)

        def on_clause(text: str):
            self._guard().spoken(text)
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
            self.pipeline.on_agent_trace = None
            self.pipeline.on_token = None

    def _pop_fresh_pending(self) -> PendingUtterance | None:
        """Next queued utterance that is still within its stale cap."""
        while self._pending and not self._closed:
            nxt = self._pending.popleft()
            age = time.monotonic() - nxt.t0
            cap = BARGE_IN_WAIT_S if nxt.kind == "barge_in" else OVERLAP_STALE_S
            if age > cap:
                label = "barge-in" if nxt.kind == "barge_in" else "stale queued"
                print(f"[asr] drop {label} utterance", flush=True)
                continue
            return nxt
        return None

    def _pcm_should_send(self, generation_id: int) -> bool:
        if not self.fsm.is_current(generation_id):
            return False
        gg = getattr(self.pipeline, "gen_guard", None)
        if gg is not None and not gg.should_enqueue_pcm(gg.generation_id):
            return False
        return True

    def _start_turn(
        self,
        audio: np.ndarray,
        nemo_result: dict | None = None,
        *,
        speech_stopped_at: float | None = None,
        kind: str = "overlap",
        turn_revision: int = 0,
        echo_suspect: bool = False,
    ):
        # Overlap (echo/backlog while a turn is in flight without interrupt)
        # is dropped. Barge-in audio captured during INTERRUPTING must wait
        # for the lock (capped at BARGE_IN_WAIT_S) instead of the 0.35s expiry.
        item = PendingUtterance(
            audio=np.asarray(audio, dtype=np.float32).reshape(-1),
            nemo_result=nemo_result,
            t0=time.monotonic(),
            kind=kind,
            speech_stopped_at=speech_stopped_at,
            turn_revision=turn_revision,
            echo_suspect=echo_suspect,
        )
        interrupting = self.fsm.state == SessionState.INTERRUPTING or kind == "barge_in"
        busy = self.fsm.state in {
            SessionState.TRANSCRIBING,
            SessionState.THINKING,
            SessionState.SPEAKING,
        } or bool(getattr(getattr(self.pipeline, "tts", None), "is_speaking", False))
        if not self._turn_lock.acquire(blocking=False):
            if interrupting:
                self._pending.append(item)
                return
            if busy:
                print("[asr] drop overlapping utterance (turn in flight)", flush=True)
                return
            self._pending.append(item)
            return

        def worker():
            try:
                self._run_turn_blocking(
                    item.audio,
                    nemo_result=item.nemo_result,
                    speech_stopped_at=item.speech_stopped_at,
                    turn_revision=item.turn_revision,
                    echo_suspect=item.echo_suspect,
                )
            finally:
                self._turn_lock.release()
            if self._closed:
                return
            nxt = self._pop_fresh_pending()
            if nxt is None:
                return
            self._start_turn(
                nxt.audio,
                nemo_result=nxt.nemo_result,
                speech_stopped_at=nxt.speech_stopped_at,
                kind=nxt.kind,
                turn_revision=nxt.turn_revision,
                echo_suspect=nxt.echo_suspect,
            )

        threading.Thread(target=worker, daemon=True, name="realtime-turn").start()

    def _run_turn_blocking(
        self,
        audio: np.ndarray,
        nemo_result: dict | None = None,
        *,
        speech_stopped_at: float | None = None,
        turn_revision: int = 0,
        echo_suspect: bool = False,
    ):
        self.pipeline.begin_turn()
        self._first_audio_ack_at = None
        self._playback_stop_ack_at = None
        ctx = self.fsm.begin_turn(speech_stopped_at=speech_stopped_at)
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
            # Too quiet to transcribe is not worth a spoken "I didn't catch
            # that": the line closes the mic again and the next try is lost too.
            self._speak_fail_closed(
                generation_id,
                reason=str(reason),
                metrics_extra={"reason": reason, **st},
                echo_suspect=echo_suspect,
                silent=str(reason).startswith(("low_rms", "low_peak", "low_speech_frac")),
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
                echo_suspect=echo_suspect,
            )
            return

        last_speak = ""
        hist = getattr(self.pipeline, "history", None)
        if hist is not None and getattr(hist, "_turns", None):
            try:
                last_speak = str(hist._turns[-1].assistant or "")
            except Exception:
                last_speak = ""
        guard = self._guard()
        # "Switch it." answering "Switch to eco mode?" shares the question's words;
        # with a question pending, a yes or no is an answer, not an echo.
        answering = self._confirmation_pending() and parse_confirmation(transcript) is not None
        # The last answer is only a suspect reference for speech that began
        # while Nova was audible or just after, and never while a route prompt
        # was what played; the word-pair guard covers every spoken clause.
        text_echo = (
            echo_suspect
            and self._audible_kind != "guidance"
            and _transcript_is_echo(transcript, last_speak)
        )
        if not answering and (guard.is_echo(transcript) or text_echo):
            print(f"[asr] skip TTS-echo transcript: {transcript!r}", flush=True)
            self._skip_turn(generation_id, "tts_echo")
            return
        said = " ".join(fold_words(transcript))
        last = self._last_user_turn
        if last and said == last[0] and time.monotonic() - last[1] < self.dedupe_user_s:
            print(f"[asr] skip repeated turn: {transcript!r}", flush=True)
            self._skip_turn(generation_id, "duplicate")
            return

        self._stop_guidance()
        self.fsm.set_thinking(generation_id)

        def on_pcm(chunk: np.ndarray, sample_rate: int):
            if not self._pcm_should_send(generation_id):
                return
            self.fsm.set_speaking(generation_id)
            self._client_playing = True
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
            self._audible_kind = "answer"
            self._send_audio(chunk, sample_rate, generation_id)

        def on_clause(text: str):
            guard.spoken(text)  # synthesized, so at least partly audible
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
        turn_trace: dict[str, dict] = {}

        def on_agent_trace(payload: dict):
            if not self.fsm.is_current(generation_id):
                return
            turn_trace["last"] = payload  # the turn's card reads it (route, results, media)
            self._schedule_send(
                {"type": "nova.agent_trace", "generation_id": generation_id, **payload}
            )

        self.pipeline.on_tool_status = on_tool_status
        self.pipeline.on_agent_trace = on_agent_trace
        self.pipeline.on_token = None
        try:
            if not self.fsm.is_current(generation_id):
                return
            # A suspect turn may be Nova's own voice: it must not seed the next prompt.
            self.pipeline.turn_trusted = not echo_suspect
            try:
                result = self.pipeline.run_text_turn(
                    transcript,
                    stt_ms=stt_ms,
                    wall0=wall0,
                    record_session=True,
                    begin=False,
                )
            finally:
                self.pipeline.turn_trusted = True
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
                for key in (
                    "barge_in_stop_ms",
                    "barge_in_detect_ms",
                    "llm_abort_ms",
                    "tts_flush_ms",
                ):
                    val = ctx.metadata.get(key)
                    if val is not None:
                        setattr(result.metrics, key, float(val))
            if turn_revision:
                result.metrics.notes.append(f"turn_revision={turn_revision}")

            metrics = result.metrics.to_dict()
            if result.assistant_text and not cancelled:
                self._schedule_send(
                    {
                        "type": "response.audio_transcript.done",
                        "transcript": result.assistant_text,
                        "generation_id": generation_id,
                    }
                )
                card = turn_card(transcript, result.assistant_text, result.tool_name,
                                 trace=turn_trace.get("last"))
                if card is not None:
                    self._schedule_send({"type": "nova.card", "generation_id": generation_id, **card})

            self.bench.add_turn(metrics)
            terminal = TurnTerminal.CANCELLED if cancelled else TurnTerminal.COMPLETED
            self.fsm.complete(generation_id, terminal, metadata=metrics)
            if not cancelled:
                self._last_user_turn = (said, time.monotonic())
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
                            "barge_in_detect_ms": metrics.get("barge_in_detect_ms"),
                            "llm_abort_ms": metrics.get("llm_abort_ms"),
                            "tts_flush_ms": metrics.get("tts_flush_ms"),
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
            self.pipeline.on_agent_trace = None
            self.pipeline.on_token = None
            self.pvad.reset()
