"""Mic mute, × cancel and display arm on the /v1/realtime door."""
from __future__ import annotations

import asyncio
import collections
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

try:  # the Pi has the Hailo SDK; the laptop gets a stub so the pipeline imports
    import hailo_platform  # noqa: F401
except ImportError:
    _hp = types.ModuleType("hailo_platform")
    _hp.VDevice = MagicMock()
    _genai = types.ModuleType("hailo_platform.genai")
    _genai.LLM = _genai.Speech2Text = _genai.Speech2TextTask = MagicMock()
    _hp.genai = _genai
    sys.modules["hailo_platform"], sys.modules["hailo_platform.genai"] = _hp, _genai

from nova_hailo.edge_harness.interrupt import GenerationGuard  # noqa: E402
from nova_hailo.session_fsm import SessionFSM, SessionState  # noqa: E402
from nova_hailo.web.realtime_session import RealtimeSession  # noqa: E402
from nova_hailo.web.session_control import SessionControl  # noqa: E402


class Hub:
    def __init__(self):
        self.events = []

    def publish_session_event_threadsafe(self, event):
        self.events.append(event)


class Vad:
    def __init__(self):
        self.fed = self.resets = 0

    def feed(self, pcm):
        self.fed += 1
        return []

    def reset(self):
        self.resets += 1


def _session():
    s = RealtimeSession.__new__(RealtimeSession)
    s.fsm = SessionFSM()
    s.pipeline = SimpleNamespace(tts=SimpleNamespace(is_speaking=False), on_abort=lambda: None,
                                 gen_guard=GenerationGuard(), abort_event=threading.Event(),
                                 stt_engine="parakeet")
    s._turn_lock = threading.Lock()
    s._pending = collections.deque(maxlen=4)
    s._closed = False
    s._response_id = "resp_x"
    s.loop = None
    s._echo_until = 0.0
    s._client_playing = False
    s._reopen_task = None
    s.reopen_ms = 300
    s._utterance_audio = None
    s._turn_revision = 0
    s._nemo_ws = None
    s._speech_started_mono = None
    s._wake_source = "none"
    s.gate_on_wake = False
    s.vad = Vad()
    s.wake = SimpleNamespace(enabled=True, available=True, last_score=0.0, barge_threshold=0.1,
                             process=MagicMock(return_value=False))
    s._uplink_frames = 0
    s._uplink_log_at = 0.0
    s.control = SessionControl(Hub())
    s._sent, s._sent_quiet = [], []
    s._schedule_send = lambda msg, mirror=True: (s._sent if mirror else s._sent_quiet).append(msg)

    async def send(msg, mirror=True):
        (s._sent if mirror else s._sent_quiet).append(msg)

    s.send = send
    return s


PCM = np.zeros(320, dtype=np.int16).tobytes()


def test_a_muted_mic_never_reaches_the_wake_word_vad_or_stt():
    s = _session()
    s._nemo_send_pcm = MagicMock()
    s.control.set_muted(True, "cockpit")
    asyncio.run(s._on_uplink_pcm(PCM))
    assert s.wake.process.call_count == 0 and s.vad.fed == 0
    assert s._nemo_send_pcm.call_count == 0


def test_muting_drops_a_half_heard_utterance_but_lets_nova_finish():
    s = _session()
    s.fsm.arm()
    s.fsm.begin_listen()
    ctx = s.fsm.begin_turn()
    s.fsm.set_thinking(ctx.generation_id)
    s.fsm.set_speaking(ctx.generation_id)
    reopen = MagicMock(done=MagicMock(return_value=False))
    s._reopen_task = reopen
    s._utterance_audio = np.ones(1600, dtype=np.float32)
    s._speech_started_mono = 1.0
    s.control.register(s)
    s.control.set_muted(True, "cockpit")
    assert s.vad.resets == 1 and s._utterance_audio is None and s._speech_started_mono is None
    assert reopen.cancel.called and s._reopen_task is None
    assert s.fsm.state == SessionState.SPEAKING                     # the reply goes on
    assert not [m for m in s._sent if m.get("type") == "response.done"]
    assert s._sent_quiet[-1] == {"type": "nova.mic", "muted": True, "source": "cockpit"}


def test_arming_a_muted_session_is_refused():
    s = _session()
    s.control.set_muted(True, "cockpit")
    for kind in ("session.arm", "ptt.down", "input_audio_buffer.arm"):
        asyncio.run(s._handle_client({"type": kind}))
    errors = [m for m in s._sent if m.get("type") == "error"]
    assert len(errors) == 3 and {e["error"]["type"] for e in errors} == {"mic_muted"}
    assert s.fsm.state == SessionState.IDLE


def test_cancel_stops_the_reply_and_drops_to_idle():
    s = _session()
    s.fsm.arm()
    s.fsm.begin_listen()
    ctx = s.fsm.begin_turn()
    s.fsm.set_thinking(ctx.generation_id)
    s.fsm.set_speaking(ctx.generation_id)
    s.control.register(s)
    assert s.control.cancel() == {"ok": True}
    assert s.fsm.state == SessionState.IDLE
    done = next(m for m in s._sent if m.get("type") == "response.done")
    assert done["response"]["status"] == "cancelled"
    assert done["response"]["metadata"]["reason"] == "ui_cancel"
    assert any(m.get("type") == "playback.cancel" for m in s._sent)


def test_a_display_arm_wakes_the_session_like_a_wake_word():
    s = _session()
    s.control.register(s)
    assert s.control.arm() == {"ok": True}
    assert s.fsm.state == SessionState.LISTENING
    wake = next(m for m in s._sent if m.get("type") == "wake.detected")
    assert wake["source"] == "cockpit"


def test_a_client_can_mute_the_cabin_with_session_update():
    s = _session()
    assert asyncio.run(s._apply_capabilities({"input": {"muted": True}})) is True
    assert s.control.muted is True and s.control.source == "client"
    asyncio.run(s._apply_capabilities({"input": {"muted": "yes"}}))
    assert s._sent[-1]["error"]["type"] == "invalid_session_update"
    assert s.control.muted is True


def test_the_session_reports_its_state_to_the_control():
    s = _session()
    s.control.register(s)
    assert s.control.status()["state"] == "IDLE"
