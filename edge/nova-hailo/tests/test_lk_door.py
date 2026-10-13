"""LiveKitDoor: the online front door inside the nova-hailo server process."""
import asyncio

import pytest

pytest.importorskip("livekit.agents")

from nova_hailo.lk import door  # noqa: E402
from nova_hailo.web.session_lease import SessionLease  # noqa: E402

_ENV = {"LIVEKIT_URL": "wss://sfu", "LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s"}


class Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


# ---- configuration --------------------------------------------------------


def test_disabled_by_default():
    ok, why = door.door_config(Cfg({}), env=_ENV)
    assert ok is None and "disabled" in why


def test_needs_credentials():
    ok, why = door.door_config(Cfg({"livekit": {"enabled": True}}), env={})
    assert ok is None and "LIVEKIT_URL" in why


def test_hostname_is_the_default_identity(monkeypatch):
    monkeypatch.setattr(door.socket, "gethostname", lambda: "Conmod-Integration1")
    ok, _ = door.door_config(Cfg({"livekit": {"enabled": True}}), env=_ENV)
    assert ok == "nova-conmod-integration1"


def test_awkward_hostnames_are_normalised(monkeypatch):
    monkeypatch.setattr(door.socket, "gethostname", lambda: "Pi_5.local")
    ok, _ = door.door_config(Cfg({"livekit": {"enabled": True}}), env=_ENV)
    assert ok == "nova-pi-5-local"


def test_explicit_device_id_overrides_the_hostname(monkeypatch):
    monkeypatch.setattr(door.socket, "gethostname", lambda: "raspberrypi")
    ok, _ = door.door_config(Cfg({"livekit": {"enabled": True, "device_id": "Lab-2"}}), env=_ENV)
    assert ok == "nova-lab-2"


def test_device_id_is_restricted_to_safe_characters():
    ok, why = door.door_config(Cfg({"livekit": {"enabled": True, "device_id": "a b/c"}}), env=_ENV)
    assert ok is None and "device_id" in why


# ---- supervision ----------------------------------------------------------


class FakeServer:
    runs = 0

    def __init__(self, *, fail):
        self.fail = fail
        self.handlers = {}
        self.closed = asyncio.Event()

    def on(self, event, cb):
        self.handlers[event] = cb

    async def run(self):
        FakeServer.runs += 1
        if self.fail:
            raise ConnectionError("sfu unreachable")
        self.handlers["worker_registered"]("AW_1", None)
        await self.closed.wait()

    async def aclose(self):
        self.closed.set()


def test_supervisor_retries_until_registered_then_stops_cleanly(monkeypatch):
    async def go():
        FakeServer.runs = 0
        attempts = iter([True, True, False])
        d = door.LiveKitDoor(
            pipeline=object(),
            lease=SessionLease(),
            agent_name="nova-q7",
            server_factory=lambda: FakeServer(fail=next(attempts)),
            backoff=(0.01, 0.02),
        )
        await d.start()
        for _ in range(100):
            if d.status == "registered":
                break
            await asyncio.sleep(0.01)
        status = d.status
        await asyncio.wait_for(d.stop(), 2)
        return status, d.status

    registered, stopped = asyncio.run(go())
    assert registered == "registered" and FakeServer.runs == 3
    assert stopped == "stopped"


# ---- one job --------------------------------------------------------------


class FakeParticipant:
    def __init__(self):
        self.sent = []

    async def publish_data(self, payload, *, reliable=True, topic=""):
        self.sent.append(payload)


class FakeRoom:
    name = "nova-q7-abc"

    def __init__(self):
        self.local_participant = FakeParticipant()

    def on(self, *a):
        pass


class FakeCtx:
    def __init__(self):
        self.room = FakeRoom()
        self.shutdown_cbs = []
        self.shut = None

    async def connect(self):
        pass

    def add_shutdown_callback(self, cb):
        self.shutdown_cbs.append(cb)

    def shutdown(self, reason=""):
        self.shut = reason


class Pipe:
    def __init__(self):
        self.cleared = 0
        self.stt = type("S", (), {"transcribe": lambda self, a, language=None: ""})()

    def clear_context(self):
        self.cleared += 1


def _door(lease, pipe):
    return door.LiveKitDoor(pipeline=pipe, lease=lease, agent_name="nova-q7", vad=object())


def test_busy_job_tells_the_browser_and_leaves_without_a_session(monkeypatch):
    started = []
    monkeypatch.setattr(door, "AgentSession", lambda **kw: started.append(kw))

    async def go():
        lease = SessionLease()
        held = lease.try_acquire("realtime")
        ctx, pipe = FakeCtx(), Pipe()
        await _door(lease, pipe).run_job(ctx)
        return ctx, pipe, held, lease

    ctx, pipe, held, lease = asyncio.run(go())
    assert started == [] and pipe.cleared == 0
    assert any("session_limit_reached" in p for p in ctx.room.local_participant.sent)
    assert ctx.shut is not None
    assert lease.holder == "realtime"


def test_job_takes_the_lease_starts_nova_and_releases_on_shutdown(monkeypatch):
    sessions = []

    class Session:
        def __init__(self, **kw):
            self.kw = kw
            sessions.append(self)

        def on(self, *a):
            pass

        class input:  # noqa: N801 - mirrors AgentSession.input
            audio = []

            @classmethod
            def set_audio_enabled(cls, on):
                cls.audio.append(on)

        async def start(self, *, agent, room, **kw):
            self.agent = agent
            self.start_kw = kw

    monkeypatch.setattr(door, "AgentSession", Session)

    async def go():
        lease = SessionLease()
        ctx, pipe = FakeCtx(), Pipe()
        await _door(lease, pipe).run_job(ctx)
        during = lease.holder
        for cb in ctx.shutdown_cbs:
            await cb()
        return during, lease.holder, pipe, sessions[0]

    during, after, pipe, sess = asyncio.run(go())
    assert during == "livekit" and after is None
    assert pipe.cleared == 1
    assert type(sess.agent).__name__ == "NovaAgent"
    assert "tts" not in sess.kw  # Nova's own Piper speaks via PCM passthrough
    assert sess.kw["turn_handling"]["endpointing"]["min_delay"] == 0.30
    # word-by-word transcript sync stalls the Pi's event loop; it must stay off
    assert sess.start_kw["room_options"].text_output.sync_transcription is False


# ---- wake gating -------------------------------------------------------------


class _GateSession:
    audio: list = []

    def __init__(self, **kw):
        type(self).audio = []
        self.input = self

    def set_audio_enabled(self, on):
        type(self).audio.append(on)

    def on(self, *a):
        pass

    async def start(self, **kw):
        pass


class _Det:
    def process(self, pcm):
        return False


async def _no_frames(ctx):
    return
    yield  # pragma: no cover


def _published(ctx):
    import json

    return [json.loads(p) for p in ctx.room.local_participant.sent]


def test_with_a_wake_word_the_session_starts_idle_and_deaf(monkeypatch):
    monkeypatch.setattr(door, "AgentSession", _GateSession)

    async def go():
        d = door.LiveKitDoor(
            pipeline=Pipe(), lease=SessionLease(), agent_name="nova-x", vad=object(),
            wake_detector=_Det(), followup_s=5.0, wake_frames=_no_frames,
        )
        ctx = FakeCtx()
        await d.run_job(ctx)
        await asyncio.sleep(0.05)
        for cb in ctx.shutdown_cbs:
            await cb()
        return ctx

    ctx = asyncio.run(go())
    assert _GateSession.audio == [False]
    assert {"type": "nova.state", "state": "idle"} in _published(ctx)


def test_without_a_wake_word_the_session_is_always_listening(monkeypatch):
    monkeypatch.setattr(door, "AgentSession", _GateSession)

    async def go():
        d = door.LiveKitDoor(pipeline=Pipe(), lease=SessionLease(), agent_name="nova-x", vad=object())
        ctx = FakeCtx()
        await d.run_job(ctx)
        await asyncio.sleep(0.05)
        return ctx

    ctx = asyncio.run(go())
    assert _GateSession.audio == []
    assert {"type": "nova.state", "state": "listening", "wake": "session"} in _published(ctx)


class _StateSession(_GateSession):
    """Keeps the handlers so a test can play AgentSession state changes."""

    handlers: dict = {}

    def __init__(self, **kw):
        super().__init__(**kw)
        type(self).handlers = {}

    def on(self, name, cb=None):
        type(self).handlers.setdefault(name, []).append(cb)

    async def start(self, *, agent, **kw):
        type(self).agent = agent

    @classmethod
    def fire(cls, name, state):
        for cb in cls.handlers.get(name, []):
            cb(type("Ev", (), {"new_state": state, "old_state": None})())


class _Hub:
    def __init__(self):
        self.events = []

    def publish_session_event_threadsafe(self, e):
        self.events.append(e)

    def session_started_threadsafe(self):
        self.events.append({"type": "observe.session", "active": True})

    def session_ended_threadsafe(self):
        self.events.append({"type": "observe.session", "active": False})


def test_an_online_caller_is_mirrored_to_observers(monkeypatch):
    monkeypatch.setattr(door, "AgentSession", _StateSession)
    hub = _Hub()

    async def go():
        d = door.LiveKitDoor(pipeline=Pipe(), lease=SessionLease(), agent_name="nova-x",
                             vad=object(), observers=hub)
        ctx = FakeCtx()
        await d.run_job(ctx)
        _StateSession.fire("agent_state_changed", "speaking")
        _StateSession.fire("agent_state_changed", "listening")
        for cb in ctx.shutdown_cbs:
            await cb()

    asyncio.run(go())
    states = [e["fsm"]["state"] for e in hub.events if e["type"] == "nova.fsm"]
    assert hub.events[0] == {"type": "observe.session", "active": True}
    assert states == ["LISTENING", "SPEAKING", "LISTENING", "IDLE"]
    assert hub.events[-1] == {"type": "observe.session", "active": False}
    assert _StateSession.agent._observe is not None


def test_without_observers_nothing_is_mirrored(monkeypatch):
    monkeypatch.setattr(door, "AgentSession", _StateSession)

    async def go():
        d = door.LiveKitDoor(pipeline=Pipe(), lease=SessionLease(), agent_name="nova-x",
                             vad=object())
        await d.run_job(FakeCtx())

    asyncio.run(go())
    assert _StateSession.agent._observe is None


class _MuteSession(_StateSession):
    input_audio: list = []

    def __init__(self, **kw):
        super().__init__(**kw)
        type(self).input_audio = []

    def set_audio_enabled(self, on):
        type(self).input_audio.append(on)


def test_an_online_caller_obeys_the_cabin_mute_cancel_and_arm(monkeypatch):
    from nova_hailo.web.session_control import SessionControl

    monkeypatch.setattr(door, "AgentSession", _MuteSession)
    hub = _Hub()
    ctl = SessionControl(hub)

    async def go():
        d = door.LiveKitDoor(pipeline=Pipe(), lease=SessionLease(), agent_name="nova-x",
                             vad=object(), wake_detector=_Det(), wake_frames=_no_frames,
                             observers=hub, session_control=ctl)
        ctx = FakeCtx()
        await d.run_job(ctx)
        agent = _MuteSession.agent
        aborted = []
        agent._abort_current = lambda: aborted.append(True)
        assert ctl.arm() == {"ok": True}
        await asyncio.sleep(0)
        opened = list(_MuteSession.input_audio)
        ctl.set_muted(True, "cockpit")
        await asyncio.sleep(0)
        muted_audio = list(_MuteSession.input_audio)
        ctl.cancel()
        await asyncio.sleep(0)
        state = ctl.status()["state"]
        for cb in ctx.shutdown_cbs:
            await cb()
        return ctx, opened, muted_audio, aborted, state

    ctx, opened, muted_audio, aborted, state = asyncio.run(go())
    assert opened[-1] is True and muted_audio[-1] is False and aborted == [True]
    assert state == "IDLE"
    assert {"type": "nova.mic", "muted": True, "source": "cockpit"} in _published(ctx)
    assert ctl.arm() == {"ok": False, "error": "mic_muted"}
    ctl.set_muted(False, "cockpit")
    assert ctl.arm() == {"ok": False, "error": "no_session"}     # unregistered at shutdown
