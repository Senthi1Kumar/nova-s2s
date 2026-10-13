"""The online front door: a LiveKit agent worker inside the nova-hailo server.

Callers: nova_hailo/web/app.py lifespan.

The worker runs as a task on the server's own event loop with thread jobs, so
the pipeline app.py built for /v1/realtime is the one that answers online
callers too -- one owner of the Hailo device, one history, one TTS queue. It
dials out to the SFU (no inbound port), registers as ``nova-<device_id>`` so the
token service can dispatch a caller to this Pi only, and keeps retrying while
the SFU is unreachable: the LAN door must never depend on the internet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from livekit import rtc
from livekit.agents import AgentServer, AgentSession, JobContext, JobExecutorType
from livekit.agents.voice.room_io import RoomOptions, TextOutputOptions

from nova_hailo.lk.agent import _log_session_events
from nova_hailo.lk.control import TOPIC, NovaControl
from nova_hailo.lk.nova_agent import NovaAgent
from nova_hailo.lk.observe_mirror import ObserveMirror
from nova_hailo.lk.stt_plugin import NovaSTT
from nova_hailo.lk.wake_gate import WakeController, WakeGate
from nova_hailo.web.settings_ops import llm_settings

logger = logging.getLogger("nova.lk")

_REQUIRED_ENV = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _normalise_hostname(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    return slug[:41].rstrip("-")


def door_config(cfg: Any, *, env: Mapping[str, str] = os.environ) -> tuple[str | None, str]:
    """(agent_name, reason). agent_name is None when the door must stay shut."""
    if not bool(cfg.get("livekit", "enabled", default=False)):
        return None, "livekit disabled (livekit.enabled is false)"
    configured = str(cfg.get("livekit", "device_id", default="") or "").strip().lower()
    # The Pi's hostname is its identity unless config names one: unique on the
    # network already, and nobody has to invent a name per device. A hostname
    # is normalised; an explicit id must already be valid, so a typo in config
    # fails loudly instead of being rewritten.
    device = configured or _normalise_hostname(socket.gethostname())
    if not _DEVICE_ID.match(device):
        return None, "livekit.device_id must be 1-41 chars of a-z, 0-9, '-'"
    missing = [k for k in _REQUIRED_ENV if not env.get(k)]
    if missing:
        return None, f"missing {', '.join(missing)} in .env"
    return f"nova-{device}", "ok"


async def _caller_mic_frames(ctx: Any) -> AsyncIterator[Any]:
    """The caller's mic as 16 kHz mono, 80 ms frames -- what the wake word wants.

    A second subscription to the same track: the AgentSession keeps its own
    for VAD and STT, and this one keeps flowing while that input is paused.
    """
    participant = await ctx.wait_for_participant()
    stream = rtc.AudioStream.from_participant(
        participant=participant,
        track_source=rtc.TrackSource.SOURCE_MICROPHONE,
        sample_rate=16000,
        num_channels=1,
        frame_size_ms=80,
    )
    try:
        async for event in stream:
            yield event.frame
    finally:
        await stream.aclose()


def _stt_engine(pipeline: Any):
    """The pipeline's resident STT when it can transcribe a clip, else load it."""
    stt = getattr(pipeline, "stt", None)
    if stt is None and hasattr(pipeline, "_ensure_stt"):
        stt = pipeline._ensure_stt()
    if not callable(getattr(stt, "transcribe", None)):
        raise RuntimeError("pipeline STT cannot transcribe a clip; online turns need one")
    return stt


class _OnlineHandle:
    """An online caller as web.session_control sees it. Calls arrive from any thread
    and run on the job's own loop."""

    def __init__(self, loop: Any, wake: WakeController, agent: Any, control: NovaControl,
                 mirror: ObserveMirror | None, session_control: Any) -> None:
        self._loop, self._wake, self._agent = loop, wake, agent
        self._control, self._mirror, self._session_control = control, mirror, session_control

    def _soon(self, fn: Callable[..., Any], *args: Any) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    def _mute(self, muted: bool) -> None:
        self._wake.set_muted(muted)
        self._control.send({"type": "nova.mic", "muted": muted,
                            "source": self._session_control.source})

    def _cancel(self) -> None:
        self._agent._abort_current()
        self._wake.close()

    def control_mute(self, muted: bool) -> None:
        self._soon(self._mute, bool(muted))

    def control_arm(self) -> None:
        self._soon(self._wake.wake, "cockpit")

    def control_cancel(self) -> None:
        self._soon(self._cancel)

    def control_state(self) -> str:
        if self._mirror is not None:
            return self._mirror.state()
        return "LISTENING" if self._wake.gate.state == "listening" else "IDLE"


class LiveKitDoor:
    def __init__(
        self,
        *,
        pipeline: Any,
        lease: Any,
        agent_name: str,
        vad: Any = None,
        server_factory: Callable[[], Any] | None = None,
        backoff: tuple[float, float] = (2.0, 60.0),
        min_silence_s: float = 0.40,
        endpoint_min_delay_s: float = 0.30,
        wake_detector: Any = None,
        followup_s: float = 5.0,
        wake_frames: Callable[[Any], AsyncIterator[Any]] | None = None,
        observers: Any = None,
        session_control: Any = None,
    ) -> None:
        # Wake gating, as on the in-car HMI: with a detector the room starts
        # idle and only "Hey Nova" (or a tap) opens a short follow-up window.
        # Without one, the room is always listening.
        self._wake_detector = wake_detector
        self._followup_s = followup_s
        self._wake_frames = wake_frames or _caller_mic_frames
        # /v1/observe hub: displays see an online caller's session like a LAN one.
        self._observers = observers
        # web.session_control: the cabin mute, a display's arm and × reach this caller.
        self._session_control = session_control
        self._pipeline = pipeline
        self._lease = lease
        self.agent_name = agent_name
        self._vad = vad
        # End of turn = VAD silence + endpointing delay. The SDK defaults
        # (0.55 s + 0.5 s) add over a second before Nova starts; these are
        # LiveKit's own "fast" endpointing values.
        self._min_silence_s = min_silence_s
        self._endpoint_min_delay_s = endpoint_min_delay_s
        self._server_factory = server_factory or self._build_server
        self._backoff = backoff
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.status = "stopped"

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._vad is None:
            from livekit.plugins import silero

            self._vad = await asyncio.to_thread(
                silero.VAD.load, min_silence_duration=self._min_silence_s
            )
        self._stopping = False
        self.status = "starting"
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            try:
                await self._server.aclose()
            except Exception:
                logger.debug("livekit worker close failed", exc_info=True)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = "stopped"

    async def _supervise(self) -> None:
        delay, cap = self._backoff
        while not self._stopping:
            server = self._server = self._server_factory()
            server.on("worker_registered", self._on_registered)
            try:
                await server.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - logged, then retried
                logger.warning("livekit worker stopped (%s); retrying in %.0fs", exc, delay)
            if self._stopping:
                break
            self.status = "retrying"
            await asyncio.sleep(delay)
            delay = min(cap, delay * 2)

    def _on_registered(self, worker_id: Any = None, _info: Any = None) -> None:
        self.status = "registered"
        print(f"LiveKit: registered as {self.agent_name} (worker {worker_id})", flush=True)

    def _build_server(self) -> AgentServer:
        server = AgentServer(
            job_executor_type=JobExecutorType.THREAD,
            num_idle_processes=0,
            # Busy is decided by the session lease and told to the caller;
            # a CPU-based "full" would leave them waiting in a silent room.
            load_fnc=lambda *_: 0.0,
        )
        server.rtc_session(agent_name=self.agent_name)(self.run_job)
        return server

    # ---- wake gating -------------------------------------------------------

    def _start_wake(
        self, ctx: Any, session: Any, control: NovaControl, mirror: ObserveMirror | None = None
    ) -> WakeController:
        detector = self._wake_detector

        def send_state(state: str, **kw: Any) -> None:
            control.send({"type": "nova.state", "state": state, **kw})
            if mirror is not None:
                mirror.on_gate(state)

        wake = WakeController(
            gate=WakeGate(followup_s=self._followup_s),
            detector=detector,
            set_audio_enabled=session.input.set_audio_enabled,
            send_state=send_state,
        )
        control.on_wake = lambda: wake.wake("tap")
        control.state_extra = lambda: {
            "wake_state": wake.gate.state,
            "wake_mode": "kws" if detector is not None else "session",
        }
        session.on("user_state_changed", lambda ev: wake.on_user_state(ev.new_state))
        session.on("agent_state_changed", lambda ev: wake.on_agent_state(ev.new_state))
        if mirror is not None:
            session.on("user_state_changed", lambda ev: mirror.on_user_state(ev.new_state))
            session.on("agent_state_changed", lambda ev: mirror.on_agent_state(ev.new_state))
        wake.start()
        if detector is None:
            return wake
        tasks = [
            asyncio.create_task(wake.run_expiry()),
            asyncio.create_task(wake.run_kws(self._wake_frames(ctx))),
        ]

        async def stop(*_: Any) -> None:
            for t in tasks:
                t.cancel()

        ctx.add_shutdown_callback(stop)
        return wake

    # ---- one online caller -------------------------------------------------

    async def run_job(self, ctx: JobContext) -> None:
        lease = self._lease.try_acquire("livekit")
        await ctx.connect()
        if lease is None:
            # Same error the LAN socket gives, so every UI shows one message.
            busy = {
                "type": "error",
                "error": {"type": "session_limit_reached", "message": "Nova is in use right now"},
            }
            await ctx.room.local_participant.publish_data(
                json.dumps(busy), reliable=True, topic=TOPIC
            )
            ctx.shutdown("session_limit_reached")
            return

        async def release(*_: Any) -> None:
            lease.release()

        ctx.add_shutdown_callback(release)
        try:
            # One room is one conversation, as one socket is on the LAN door.
            clear = getattr(self._pipeline, "clear_context", None)
            if clear is not None:
                clear()
            stt = NovaSTT(await asyncio.to_thread(_stt_engine, self._pipeline))
            control = NovaControl(ctx.room, self._pipeline)
            control.attach()
            # No tts: Nova's own Piper speaks, carried as PCM by NovaAgent.
            session = AgentSession(
                stt=stt,
                vad=self._vad,
                turn_handling={
                    "endpointing": {"min_delay": self._endpoint_min_delay_s, "max_delay": 3.0}
                },
            )
            _log_session_events(session)
            mirror = ObserveMirror(self._observers) if self._observers is not None else None
            agent = NovaAgent(self._pipeline, on_event=control.send, observe=mirror)
            await session.start(
                agent=agent,
                room=ctx.room,
                # Word-by-word transcript sync runs an STFT over every agent
                # frame on the event loop -- 1.3 s stalls on the Pi. The UI
                # shows whole clauses, so publish transcripts unsynchronised.
                room_options=RoomOptions(text_output=TextOutputOptions(sync_transcription=False)),
            )
            if mirror is not None:
                mirror.started()
                control.on_llm_changed = lambda: mirror.event(
                    {"type": "nova.settings", "llm": llm_settings(self._pipeline)})

                async def end_mirror(*_: Any) -> None:
                    mirror.ended()

                ctx.add_shutdown_callback(end_mirror)
            wake = self._start_wake(ctx, session, control, mirror)
            if self._session_control is not None:
                handle = _OnlineHandle(asyncio.get_running_loop(), wake, agent, control,
                                       mirror, self._session_control)
                control.on_mic_set = lambda muted: self._session_control.set_muted(muted, "livekit")
                unregister = self._session_control.register(handle)

                async def leave_control(*_: Any) -> None:
                    unregister()

                ctx.add_shutdown_callback(leave_control)
            logger.info("nova online session in room=%s", ctx.room.name)
        except Exception:
            lease.release()
            raise
