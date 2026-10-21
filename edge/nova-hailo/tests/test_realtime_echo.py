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


# ---- Follow-ups during a route -------------------------------------------------
# Utterances a Pi session dropped while a route played: route prompts are not
# conversation, a soft voice is not noise, and a request that only reuses Nova's
# words is not her echo.

import pytest  # noqa: E402

from nova_hailo.audio_gate import _quiet_speech, accept_utterance  # noqa: E402
from nova_hailo.web.realtime_session import _transcript_is_echo  # noqa: E402

GUIDANCE = "In 300 metres, turn right onto Castro Street."


@pytest.mark.parametrize(("user", "nova"), [
    ("Yes, switch the dry mode to sport mode.", "Switch to sport mode while driving?"),
    ("Can you down the ambient light brightness to thirty percent.", "Ambient light set to purple."),
    ("Hey, decrease the ambient light percent to thirty.", "Ambient light set to purple."),
])
def test_a_request_that_reuses_the_last_answers_words_is_not_echo(user, nova):
    assert _transcript_is_echo(user, nova) is False


@pytest.mark.parametrize("st", [
    {"sec": 4.48, "rms": 0.01412, "peak": 0.0974, "speech_frac": 0.443},
    {"sec": 7.728, "rms": 0.01462, "peak": 0.13087, "speech_frac": 0.401},
    {"sec": 6.144, "rms": 0.01521, "peak": 0.09153, "speech_frac": 0.441},
    {"sec": 2.432, "rms": 0.01474, "peak": 0.09232, "speech_frac": 0.358},
    {"sec": 1.856, "rms": 0.01403, "peak": 0.09562, "speech_frac": 0.246},
    {"sec": 1.792, "rms": 0.01355, "peak": 0.10008, "speech_frac": 0.22},
    {"sec": 3.936, "rms": 0.01292, "peak": 0.11576, "speech_frac": 0.275},
    {"sec": 4.08, "rms": 0.009, "peak": 0.05207, "speech_frac": 0.125},
])
def test_soft_speech_the_pi_rejected_now_counts_as_speech(st):
    assert _quiet_speech(st)


def test_quiet_noise_is_still_rejected_whatever_the_configured_floor():
    noise = np.random.default_rng(0).standard_normal(16000).astype(np.float32) * 0.008
    ok, reason, _ = accept_utterance(noise, min_rms=0.016)
    assert not ok and "low_rms" in reason


def test_soft_speech_passes_a_strict_configured_floor():
    t = np.arange(32000, dtype=np.float32) / 16000.0
    env = np.sin(2 * np.pi * 3 * t) * 0.5 + 0.5
    # Voiced pulses: a speech-like crest (peak 0.1 over rms 0.014), as logged.
    soft = (np.sin(2 * np.pi * 120 * t) ** 63 * env ** 2 * 0.1).astype(np.float32)
    ok, reason, st = accept_utterance(soft, min_rms=0.016)
    assert st["rms"] < 0.016 and ok, (reason, st)


def _route_session():
    s = _session()
    s.echo_suspect_s = 1.5
    p = s.pipeline
    pending = [GUIDANCE]
    p.nav_feed = SimpleNamespace(guidance_waiting=lambda: bool(pending),
                                 take_guidance=lambda: pending.pop(0) if pending else None)
    calls = []
    plain = p._speak_tool_line

    def speak(line, **kw):
        calls.append(kw)
        if p.on_audio_pcm is not None:
            p.on_audio_pcm(np.zeros(1600, dtype=np.float32), 16000)
        return plain(line, **kw)

    p._speak_tool_line = speak
    return s, calls


def test_a_route_prompt_stays_out_of_history():
    s, calls = _route_session()
    s.pipeline.history.add("take me to Mountain View", "Starting the route to Mountain View.")
    assert s._speak_nav_guidance() is True
    assert calls[0]["add_history"] is False
    assert s.pipeline.history._turns[-1].assistant == "Starting the route to Mountain View."
    assert s._audible_kind == "guidance"


def test_talking_over_a_route_prompt_is_a_turn_and_stops_the_prompt():
    s, _ = _route_session()
    s._speak_nav_guidance()
    s._client_playing = True
    say(s, "Turn right on Castro Street?", echo_suspect=True)
    assert s.pipeline.llm_calls == ["Turn right on Castro Street?"]
    cancels = [m for m in s._sent if m.get("type") == "playback.cancel"]
    assert cancels and cancels[-1]["reason"] == "user_turn"
    assert s._audible_kind == "answer"


def test_the_prompt_heard_back_is_still_dropped():
    s, _ = _route_session()
    s._speak_nav_guidance()
    say(s, "in 300 metres turn right onto castro street", echo_suspect=True)
    assert s.pipeline.llm_calls == [] and skipped(s) == ["tts_echo"]


def test_a_rephrase_well_after_the_answer_is_kept():
    s = _session()
    say(s, "set the temperature to 23")
    # Not a suspect: spoken well after playback, so the last answer is no reference.
    say(s, "driver side to 23 degrees and passenger to 21", echo_suspect=False)
    assert len(s.pipeline.llm_calls) == 2


def test_a_quiet_utterance_is_rejected_without_a_spoken_line():
    s = _session()
    s.gate_min_rms, s.gate_min_peak, s.gate_min_sec, s.gate_min_speech_frac = 0.016, 0.045, 0.4, 0.15
    quiet = np.random.default_rng(1).standard_normal(16000).astype(np.float32) * 0.004
    s._run_turn_blocking(quiet)
    assert s.pipeline.spoken_lines == [] and skipped(s) == ["low_energy"]


def test_route_prompt_transcripts_are_flagged_as_guidance():
    s, _ = _route_session()
    assert s._speak_nav_guidance() is True
    texts = [m for m in s._sent if str(m.get("type", "")).startswith("response.audio_transcript.")]
    assert {m["type"] for m in texts} == {"response.audio_transcript.delta",
                                          "response.audio_transcript.done"}
    assert all(m.get("nav_guidance") is True for m in texts)


def test_a_normal_reply_transcript_is_not_flagged_as_guidance():
    s = _session()
    say(s, "set the temperature to 23")
    texts = [m for m in s._sent if str(m.get("type", "")).startswith("response.audio_transcript.")]
    assert not any("nav_guidance" in m for m in texts)


def test_a_research_announcement_is_not_flagged_as_guidance():
    s = _session()
    assert s._speak_research_result({"announce": "The research on Mars is ready."}) is True
    texts = [m for m in s._sent if str(m.get("type", "")).startswith("response.audio_transcript.")]
    assert {m["type"] for m in texts} == {"response.audio_transcript.delta",
                                          "response.audio_transcript.done"}
    assert not any("nav_guidance" in m for m in texts)
