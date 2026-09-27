"""Device-facing realtime API: frames, capabilities, echo, resample — no Hailo."""
from __future__ import annotations

import asyncio
import base64
import collections
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

# pipeline.py / llm.py / stt.py import hailo_platform at module load.
_hp = types.ModuleType("hailo_platform")
_hp.VDevice = MagicMock()
_genai = types.ModuleType("hailo_platform.genai")
_genai.LLM = MagicMock()
_genai.Speech2Text = MagicMock()
_genai.Speech2TextTask = MagicMock()
_hp.genai = _genai
sys.modules.setdefault("hailo_platform", _hp)
sys.modules.setdefault("hailo_platform.genai", _genai)
_fa = types.ModuleType("fastapi")
_fa.WebSocket = MagicMock()
sys.modules.setdefault("fastapi", _fa)
_st = types.ModuleType("starlette")
_st_ws = types.ModuleType("starlette.websockets")
_st_ws.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})


class _WebSocketState:
    CONNECTED = 1
    DISCONNECTED = 0


_st_ws.WebSocketState = _WebSocketState
sys.modules.setdefault("starlette", _st)
sys.modules.setdefault("starlette.websockets", _st_ws)
sys.modules.setdefault("sounddevice", MagicMock())

import numpy as np

from nova_hailo.edge_harness.interrupt import GenerationGuard
from nova_hailo.session_fsm import SessionFSM, SessionState
from nova_hailo.web.realtime_session import RealtimeSession


class _Wake:
    def __init__(self, available: bool = True):
        self.enabled = available
        self.available = available
        self.last_score = 0.0
        self.barge_threshold = 0.10
        self.calls: list[int] = []

    def process(self, pcm, threshold=None, ignore_cooldown=False):
        self.calls.append(len(pcm))
        return False

    def status(self):
        return {"enabled": self.enabled}


class _Vad:
    def __init__(self, events=None):
        self.fed = 0
        self.events = list(events or [])
        self.barge_marked = False

    def feed(self, pcm):
        self.fed += 1
        return list(self.events)

    def reset(self):
        return None

    def set_threshold(self, thr):
        self.threshold = thr

    def mark_barge_in(self):
        self.barge_marked = True


def _device_session(*, wake_available: bool = True, vad_events=None) -> RealtimeSession:
    s = RealtimeSession.__new__(RealtimeSession)
    s.session_id = "sess_test"
    s.fsm = SessionFSM(session_idle_sec=45)
    s.fsm._expire_listening = True
    s.pipeline = SimpleNamespace(
        tts=SimpleNamespace(is_speaking=False),
        on_abort=lambda: None,
        gen_guard=GenerationGuard(),
        abort_event=threading.Event(),
        stt_engine="whisper_hef",
    )
    s._turn_lock = threading.Lock()
    s._pending = collections.deque(maxlen=4)
    s._closed = False
    s._response_id = None
    s.loop = None
    s._echo_until = 0.0
    s._client_playing = False
    s._reopen_task = None
    s.reopen_ms = 300
    s._utterance_audio = None
    s._turn_revision = 0
    s._nemo_ws = None
    s.echo_tail_ms = 0
    s.echo_max_ms = 12000
    s._echo_logged = False
    s._ns_held = False
    s._echo_hold_started = 0.0
    s._uplink_frames = 0
    s._uplink_log_at = float("inf")  # silence the 5 s uplink log line
    s._wake_peak = 0.0
    s.ns = None
    s.barge_in_while_speaking = False
    s.pvad = SimpleNamespace(available=False, feed_pcm16=lambda pcm: False, status=lambda: {})
    s.vad = _Vad(vad_events)
    s.wake = _Wake(available=wake_available)
    s.gate_on_wake = wake_available
    s.require_arm = wake_available
    s._ptt_armed = False
    s._logged_ignore_auto_arm = False
    s._wake_source = "server" if wake_available else "none"
    s._client_aec = None
    s._output_rate = None
    s._sent = []
    s._schedule_send = lambda msg: s._sent.append(msg)

    async def _send(ev):
        s._sent.append(ev)

    s.send = _send
    s.barges = []
    s._barge_in = lambda reason, **kw: s.barges.append(reason)

    async def _noop(*a, **kw):
        return None

    s._start_nemo_stream = _noop
    s._nemo_send_pcm = _noop
    s._schedule_utterance_commit = _noop
    s._cancel_reopen = lambda: None
    return s


def _run(coro):
    return asyncio.run(coro)


def _errors(s) -> list[str]:
    return [m["error"]["type"] for m in s._sent if m.get("type") == "error"]


# ---- Task 1: frames -------------------------------------------------------


def test_binary_frame_and_json_append_reach_same_uplink():
    s = _device_session()
    got: list[bytes] = []

    async def _rec(pcm):
        got.append(pcm)

    s._on_uplink_pcm = _rec
    pcm = np.arange(320, dtype=np.int16).tobytes()
    assert _run(s._dispatch_frame({"type": "websocket.receive", "bytes": pcm})) is True
    text = '{"type":"input_audio_buffer.append","audio":"%s"}' % base64.b64encode(pcm).decode()
    assert _run(s._dispatch_frame({"type": "websocket.receive", "text": text})) is True
    assert got == [pcm, pcm]


def test_odd_binary_frame_is_rejected_and_socket_stays_open():
    s = _device_session()
    called = []

    async def _rec(pcm):
        called.append(pcm)

    s._on_uplink_pcm = _rec
    assert _run(s._dispatch_frame({"type": "websocket.receive", "bytes": b"\x00\x01\x02"})) is True
    assert called == []
    assert _errors(s) == ["invalid_audio_frame"]


def test_disconnect_message_ends_the_loop():
    s = _device_session()
    assert _run(s._dispatch_frame({"type": "websocket.disconnect", "code": 1000})) is False


def test_non_object_json_is_ignored():
    s = _device_session()
    assert _run(s._dispatch_frame({"type": "websocket.receive", "text": "[1,2]"})) is True
    assert s._sent == []


# ---- Task 2: capabilities -------------------------------------------------


def _update(s, session: dict):
    _run(s._handle_client({"type": "session.update", "session": session}))


def _pcm(n=320):
    return np.zeros(n, dtype=np.int16).tobytes()


def test_capabilities_absent_keep_defaults():
    s = _device_session()
    _update(s, {"audio": {"input": {"turn_detection": {"threshold": 0.5}}}})
    assert s._wake_source == "server"
    assert s._client_aec is None
    assert s._output_rate is None
    assert _errors(s) == []


def test_device_wake_skips_server_kws():
    s = _device_session()
    _update(s, {"input": {"wake": "device"}})
    _run(s._handle_client({"type": "session.arm"}))
    assert s.fsm.state == SessionState.LISTENING
    _run(s._on_uplink_pcm(_pcm()))
    assert s.wake.calls == []


def test_device_wake_idle_audio_dropped_until_arm():
    s = _device_session(vad_events=[("speech_started", None)])
    _update(s, {"input": {"wake": "device"}})
    _run(s._on_uplink_pcm(_pcm()))
    assert s.fsm.state == SessionState.IDLE
    assert not any(m.get("type") == "input_audio_buffer.speech_started" for m in s._sent)
    _run(s._handle_client({"type": "session.arm"}))
    wake = next(m for m in s._sent if m.get("type") == "wake.detected")
    assert wake["source"] == "device"
    assert wake["score"] is None
    assert s.fsm.state == SessionState.LISTENING


def test_device_wake_forces_idle_when_session_started_listening():
    s = _device_session(wake_available=False)
    s.fsm.arm()
    s.fsm.begin_listen()  # what run() does on a non-KWS profile
    assert s.fsm.state == SessionState.LISTENING
    _update(s, {"input": {"wake": "device"}})
    assert s.fsm.state == SessionState.IDLE
    assert s.gate_on_wake is True


def test_device_arm_during_playback_barges_in():
    s = _device_session()
    _update(s, {"input": {"wake": "device", "echo_cancellation": True}})
    s._client_playing = True
    _run(s._handle_client({"type": "session.arm"}))
    assert s.barges == ["wake_word"]


def test_echo_false_gates_uplink_even_if_server_allows_barge_in():
    s = _device_session(vad_events=[("speech_started", None)])
    s.barge_in_while_speaking = True
    _update(s, {"input": {"echo_cancellation": False}})
    s.fsm.arm()
    s.fsm.begin_listen()
    s._client_playing = True
    _run(s._on_uplink_pcm(_pcm()))
    assert not any(m.get("type") == "input_audio_buffer.speech_started" for m in s._sent)
    assert s.barges == []


def test_echo_false_skips_wake_scoring_while_audible():
    s = _device_session()
    _update(s, {"input": {"echo_cancellation": False}})
    s._client_playing = True
    _run(s._on_uplink_pcm(_pcm()))
    assert s.wake.calls == []
    s._client_playing = False
    _run(s._on_uplink_pcm(_pcm()))
    assert len(s.wake.calls) == 1


def test_echo_true_allows_voice_barge_in():
    s = _device_session(vad_events=[("speech_started", None)])
    s.barge_in_while_speaking = False
    _update(s, {"input": {"echo_cancellation": True}})
    s.fsm.arm()
    s.fsm.begin_listen()
    s._client_playing = True
    _run(s._on_uplink_pcm(_pcm()))
    assert any(m.get("type") == "input_audio_buffer.speech_started" for m in s._sent)
    assert s.barges == ["vad_speech_started"]


def test_echo_true_client_cancel_barges_in_despite_server_default():
    s = _device_session()
    s.barge_in_while_speaking = False
    _update(s, {"input": {"echo_cancellation": True}})
    s._client_playing = True
    s._echo_until = 10**12  # deep inside the echo window
    _run(s._handle_client({"type": "response.cancel"}))
    assert s.barges == ["client_cancel"]


def test_invalid_capabilities_are_rejected_without_state_change():
    s = _device_session()
    _update(s, {"input": {"wake": "both"}})
    _update(s, {"input": {"echo_cancellation": "yes"}})
    assert _errors(s) == ["invalid_session_update", "invalid_session_update"]
    assert s._wake_source == "server"
    assert s._client_aec is None


def test_server_wake_rejected_when_pi_has_no_kws():
    s = _device_session(wake_available=False)
    _update(s, {"input": {"wake": "server"}})
    assert _errors(s) == ["invalid_session_update"]
    assert s._wake_source == "none"


# ---- Task 3: output rate + session.created ---------------------------------

from nova_hailo.web.realtime_session import PROTOCOL_VERSION, _resample_f32


def test_resample_preserves_duration_per_chunk():
    for n in (1, 2205, 11025, 22050, 33333):
        x = np.random.default_rng(0).uniform(-0.5, 0.5, n).astype(np.float32)
        y = _resample_f32(x, 22050, 16000)
        assert abs(y.size - round(n * 16000 / 22050)) <= 1
        assert y.dtype == np.float32


def test_resample_same_rate_is_identity():
    x = np.linspace(-1, 1, 100, dtype=np.float32)
    assert _resample_f32(x, 16000, 16000) is x


def test_audio_delta_native_rate_when_not_requested():
    s = _device_session()
    (ev,) = s._audio_delta_events(np.zeros(2205, dtype=np.float32), 22050, 7)
    assert ev["type"] == "response.audio.delta"
    assert ev["sample_rate"] == 22050
    assert ev["generation_id"] == 7
    assert len(base64.b64decode(ev["delta"])) == 2205 * 2


def test_audio_delta_resampled_to_requested_rate():
    s = _device_session()
    _update(s, {"audio": {"output": {"sample_rate": 16000}}})
    evs = s._audio_delta_events(np.zeros(22050, dtype=np.float32), 22050, 1)
    assert {ev["sample_rate"] for ev in evs} == {16000}
    assert sum(len(base64.b64decode(ev["delta"])) for ev in evs) == 16000 * 2


def test_unsupported_output_rate_is_rejected():
    s = _device_session()
    _update(s, {"audio": {"output": {"sample_rate": 8000}}})
    _update(s, {"audio": {"output": {"sample_rate": True}}})
    assert _errors(s) == ["unsupported_sample_rate", "unsupported_sample_rate"]
    assert s._output_rate is None


def test_session_created_reports_protocol_and_audio():
    s = _device_session()
    s.pipeline.llm_settings_snapshot = lambda: {}
    p = s._session_created_payload(greeting="")
    assert p["type"] == "session.created"
    sess = p["session"]
    assert sess["protocol"] == PROTOCOL_VERSION
    assert sess["model"] == "nova-hailo"
    assert sess["audio"]["input"] == {"format": "pcm16", "sample_rate": 16000, "channels": 1}
    assert sess["audio"]["output"]["format"] == "pcm16"
    assert sess["wake_source"] == "server"


# ---- Final-review fixes -----------------------------------------------------


def test_audio_deltas_are_sliced_to_200ms():
    # A whole TTS sentence in one delta overflows small-device receive buffers.
    s = _device_session()
    evs = s._audio_delta_events(np.zeros(16000 * 3, dtype=np.float32), 16000, 5)
    sizes = [len(base64.b64decode(e["delta"])) for e in evs]
    assert max(sizes) <= 6400
    assert sum(sizes) == 16000 * 3 * 2
    assert all(e["generation_id"] == 5 and e["sample_rate"] == 16000 for e in evs)


def test_resampled_deltas_are_sliced_at_output_rate():
    s = _device_session()
    _update(s, {"audio": {"output": {"sample_rate": 16000}}})
    evs = s._audio_delta_events(np.zeros(22050 * 2, dtype=np.float32), 22050, 1)
    sizes = [len(base64.b64decode(e["delta"])) for e in evs]
    assert max(sizes) <= 6400
    assert abs(sum(sizes) - 32000 * 2) <= 2


def test_speaking_releases_after_playback_stopped_without_uplink():
    # A no-AEC device may pause its mic until the server leaves SPEAKING, so the
    # release cannot wait for the next uplink frame.
    async def go():
        s = _device_session()
        s.loop = asyncio.get_running_loop()
        s.fsm.state = SessionState.SPEAKING
        s._client_playing = True
        await s._handle_client({"type": "playback.stopped"})
        await asyncio.sleep(2.0)  # PLAYBACK_STOPPED_HOLD_S (1.8 s) + margin
        return s.fsm.state

    assert _run(go()) == SessionState.LISTENING


def test_malformed_session_update_keeps_socket_open():
    s = _device_session()
    for bad in ({"input": "x"}, {"audio": "x"}, {"audio": {"output": 7}}):
        _update(s, bad)
    _run(s._handle_client({"type": "session.update", "session": "x"}))
    assert _errors(s) == ["invalid_session_update"] * 4


def test_follow_up_window_never_expires_mid_utterance():
    # Seen on the Pi: frames arrived late, the 8 s window passed while the
    # user was mid-sentence, the session fell to IDLE and a device-wake
    # question was dropped without a word.
    s = _device_session(vad_events=[("speech_started", None)])
    _update(s, {"input": {"wake": "device"}})
    _run(s._handle_client({"type": "session.arm"}))
    _run(s._on_uplink_pcm(_pcm()))           # speech starts
    s.vad.events = []                         # ...and continues
    s.fsm._armed_until = 0.0                  # deadline passes during speech
    _run(s._on_uplink_pcm(_pcm()))
    assert s.fsm.state == SessionState.LISTENING
    s.vad.events = [("speech_stopped", None)]
    _run(s._on_uplink_pcm(_pcm()))            # utterance over
    s.vad.events = []
    _run(s._on_uplink_pcm(_pcm()))
    assert s.fsm.state == SessionState.IDLE   # expiry works again afterwards


# ---- One WebSocket is one conversation ----------------------------------------


def test_new_connection_starts_a_new_conversation():
    from nova_hailo.context_history import ConversationHistory
    from nova_hailo.pipeline import NovaPipeline
    from nova_hailo.web.realtime_session import _transcript_is_echo

    # Left over from the previous connection: its last reply (the echo filter's
    # reference) and a question saved for replay on the next turn.
    hist = ConversationHistory(max_turns=5)
    hist.add("Hey there, can you hear me?",
             "Yeah, I hear you loud and clear. How can I help you today?")
    hist._unanswered = "Can you tell me the latest news today in Tamil Nadu?"
    assert _transcript_is_echo("Can you hear me?", hist._turns[-1].assistant)

    s = _device_session()
    events: list[str] = []
    p = s.pipeline
    p.history = hist
    p.llm = SimpleNamespace(clear=lambda: events.append("llm.clear"))
    p.auth = SimpleNamespace(reset_session=lambda: events.append("auth.reset"))
    p._native_seeded = True
    p.clear_context = types.MethodType(NovaPipeline.clear_context, p)

    class _WS:
        async def accept(self):
            events.append("accept")

        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1000}

    async def _send(ev):
        events.append(ev.get("type"))

    s.ws = _WS()
    s.send = _send
    s._nemo_stream_result = None
    _run(s.run())

    assert list(hist._turns) == [] and hist._unanswered is None
    assert p._native_seeded is False          # the on-device model is re-seeded next turn
    assert "auth.reset" in events             # no payment confirmation carries over
    assert events.index("llm.clear") < events.index("session.created")
