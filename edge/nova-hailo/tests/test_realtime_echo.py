"""Nova must not hear herself: echo suspects, the fail-closed line, repeats, the loop."""
from __future__ import annotations

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

from nova_hailo.context_history import ConversationHistory  # noqa: E402
from nova_hailo.edge_harness.interrupt import GenerationGuard  # noqa: E402
from nova_hailo.metrics import TurnMetrics  # noqa: E402
from nova_hailo.session_fsm import SessionFSM, TurnTerminal  # noqa: E402
from nova_hailo.web.realtime_session import RealtimeSession  # noqa: E402

REPLY = "Driver side set to 23 degrees."


class Pipeline:
    def __init__(self):
        self.llm_calls, self.spoken_lines = [], []
        self.history = ConversationHistory(max_turns=3)
        self.tts = SimpleNamespace(is_speaking=False)
        self.abort_event = threading.Event()
        self.gen_guard = GenerationGuard()
        self.stt_engine, self.llm = "parakeet", None
        self.on_validated_clause = self.on_audio_pcm = None
        self.turn_trusted_seen = []

    def begin_turn(self):
        pass

    sequential_stt = False

    def _ensure_stt(self):
        return SimpleNamespace(transcribe=lambda audio: "")   # offline STT hears nothing too

    def _with_genai(self, fn):
        return fn()

    def on_abort(self):
        pass

    def run_text_turn(self, text, **kw):
        self.llm_calls.append(text)
        self.turn_trusted_seen.append(getattr(self, "turn_trusted", True))
        self.on_validated_clause(REPLY)
        return SimpleNamespace(metrics=TurnMetrics(), assistant_text=REPLY, tool_name=None)

    def _speak_tool_line(self, line, **kw):
        self.spoken_lines.append(line)
        self.on_validated_clause(line)
        return SimpleNamespace(metrics=TurnMetrics(), assistant_text=line, tool_name=None)


def _session():
    s = RealtimeSession.__new__(RealtimeSession)
    s.fsm = SessionFSM()
    s.pipeline = Pipeline()
    s._turn_lock = threading.Lock()
    s._pending = collections.deque(maxlen=4)
    s._closed = False
    s._response_id = "resp_x"
    s.loop = None
    s._echo_until = 0.0
    s.echo_max_ms = 12000
    s._client_playing = False
    s._first_audio_ack_at = s._playback_stop_ack_at = None
    s._last_fail_closed_mono = float("-inf")
    s.gate_min_rms = s.gate_min_peak = s.gate_min_sec = s.gate_min_speech_frac = 0.0
    s.nemo_wait_floor_s, s.nemo_wait_cap_s, s.nemo_wait_base_s, s.nemo_wait_scale = 0.0, 0.1, 0.0, 0.0
    s.bench = SimpleNamespace(add_turn=lambda m: None, summary=lambda: {})
    s.pvad = SimpleNamespace(reset=lambda: None)
    s._sent = []
    s._schedule_send = lambda msg, mirror=True: s._sent.append(msg)
    s._send_audio = lambda *a, **k: None
    return s


AUDIO = np.full(16000, 0.1, dtype=np.float32)


def say(s, transcript, **kw):
    ev = threading.Event()
    ev.set()
    s._run_turn_blocking(AUDIO, nemo_result={"event": ev, "text": transcript}, **kw)


def skipped(s):
    return [m["response"]["metadata"].get("reason") for m in s._sent
            if m.get("type") == "response.done" and m["response"]["metadata"].get("skipped")]


def test_nova_hearing_her_own_reply_costs_no_llm_call():
    s = _session()
    say(s, "set the temperature to 23")
    say(s, "driver side set to 23 degrees")          # the speaker, picked up by the mic
    assert s.pipeline.llm_calls == ["set the temperature to 23"]
    assert skipped(s) == ["tts_echo"]


def test_a_repeated_turn_right_after_its_answer_is_dropped():
    s = _session()
    say(s, "set the temperature to 23")
    say(s, "set the temperature to 23")
    assert s.pipeline.llm_calls == ["set the temperature to 23"]
    assert skipped(s) == ["duplicate"]


def test_the_same_request_later_runs_again():
    s = _session()
    s.dedupe_user_s = 0.0
    say(s, "set the temperature to 23")
    say(s, "set the temperature to 23")
    assert len(s.pipeline.llm_calls) == 2


def test_a_suspect_turn_does_not_seed_the_next_prompt():
    s = _session()
    say(s, "what is the weather on mars", echo_suspect=True)
    assert s.pipeline.turn_trusted_seen == [False]
    assert getattr(s.pipeline, "turn_trusted", True) is True     # reset after the turn


def test_an_echo_suspect_never_hears_the_fail_closed_line():
    s = _session()
    say(s, "", echo_suspect=True)                                 # STT heard nothing usable
    assert s.pipeline.spoken_lines == [] and skipped(s) == ["echo_suspect"]
    assert s.fsm.last_terminal == TurnTerminal.REJECTED


def test_the_fail_closed_line_is_not_repeated_within_the_cooldown():
    s = _session()
    s.fail_closed_cooldown_s = 6.0
    say(s, "")
    say(s, "")
    assert len(s.pipeline.spoken_lines) == 1 and skipped(s) == ["fail_closed_cooldown"]


def test_speech_soon_after_playback_or_a_barge_is_suspect():
    s = _session()
    s.echo_suspect_s = 1.5
    s._last_audible_mono, s._last_barge_mono = 10.0, 0.0
    assert s._echo_suspect(9.0) and s._echo_suspect(11.0) and not s._echo_suspect(12.0)
    s._last_barge_mono = 20.0
    assert s._echo_suspect(20.5) and not s._echo_suspect(22.0)
    assert not s._echo_suspect(None)


def test_an_answer_to_a_pending_confirmation_is_never_taken_for_echo():
    s = _session()
    s._guard().spoken("Switch to eco mode while driving?")
    s.pipeline.oem_gateway = SimpleNamespace(vehicle=SimpleNamespace(pending=object()))
    s.pipeline.history.add("change the drive mode to eco", "Switch to eco mode while driving?")
    say(s, "Switch it.")
    assert s.pipeline.llm_calls == ["Switch it."]
