"""Barge-in queue, VAD speech_stopped stamp, cloud abort, reopen — no Hailo."""
from __future__ import annotations

import asyncio
import collections
import sys
import threading
import time
from types import SimpleNamespace
import types
from unittest.mock import MagicMock

# pipeline.py / llm.py / stt.py import hailo_platform at module load.
_hp = types.ModuleType("hailo_platform")
_hp.VDevice = MagicMock()
_genai = types.ModuleType("hailo_platform.genai")
_genai.LLM = MagicMock()
_genai.Speech2Text = MagicMock()
_genai.Speech2TextTask = MagicMock()
_hp.genai = _genai
sys.modules["hailo_platform"] = _hp
sys.modules["hailo_platform.genai"] = _genai

_fa = types.ModuleType("fastapi")
_fa.WebSocket = MagicMock()
sys.modules["fastapi"] = _fa
_st = types.ModuleType("starlette")
_st_ws = types.ModuleType("starlette.websockets")
_st_ws.WebSocketDisconnect = type("WebSocketDisconnect", (Exception,), {})

class _WebSocketState:
    CONNECTED = 1
    DISCONNECTED = 0

_st_ws.WebSocketState = _WebSocketState
sys.modules["starlette"] = _st
sys.modules["starlette.websockets"] = _st_ws
sys.modules.setdefault("sounddevice", MagicMock())

import httpx
import numpy as np

from nova_hailo.backends.openrouter_llm import OpenRouterLLM
from nova_hailo.edge_harness.interrupt import GenerationGuard
from nova_hailo.metrics import TurnMetrics
from nova_hailo.pipeline import NovaPipeline
from nova_hailo.session_fsm import SessionFSM, SessionState
from nova_hailo.web.realtime_session import (
    BARGE_IN_WAIT_S,
    OVERLAP_STALE_S,
    PendingUtterance,
    RealtimeSession,
    should_interrupt_on_user_speech,
)


def test_speech_stopped_at_is_vad_stamp_not_begin_turn_wall():
    fsm = SessionFSM()
    vad_t = time.perf_counter()
    stamped = fsm.note_speech_stopped(when=vad_t)
    time.sleep(0.02)
    ctx = fsm.begin_turn()
    assert stamped == vad_t
    assert ctx.speech_stopped_at == vad_t
    assert ctx.started_at >= ctx.speech_stopped_at


def test_begin_turn_without_vad_stamp_leaves_speech_stopped_none():
    fsm = SessionFSM()
    ctx = fsm.begin_turn()
    assert ctx.speech_stopped_at is None


def test_begin_turn_copies_explicit_speech_stopped_at():
    fsm = SessionFSM()
    fsm.note_speech_stopped(when=1.0)
    ctx = fsm.begin_turn(speech_stopped_at=9.5)
    assert ctx.speech_stopped_at == 9.5


def test_generation_guard_discard_window_until_begin():
    g = GenerationGuard()
    gid = g.begin()
    g.invalidate("barge_in")
    assert g.discarding is True
    assert g.should_enqueue_pcm(gid) is False
    assert g.should_enqueue_pcm(g.generation_id) is False
    nxt = g.begin()
    assert g.discarding is False
    assert g.should_enqueue_pcm(nxt) is True
    assert g.should_enqueue_pcm(gid) is False


def _session() -> RealtimeSession:
    s = RealtimeSession.__new__(RealtimeSession)
    s.fsm = SessionFSM()
    s.pipeline = SimpleNamespace(
        tts=SimpleNamespace(is_speaking=True),
        on_abort=lambda: None,
        gen_guard=GenerationGuard(),
        abort_event=threading.Event(),
        stt_engine="whisper_hef",
    )
    s._turn_lock = threading.Lock()
    s._pending = collections.deque(maxlen=4)
    s._closed = False
    s._response_id = "resp_x"
    s.loop = None
    s._echo_until = 1.0
    s._client_playing = False
    s._reopen_task = None
    s.reopen_ms = 300
    s._utterance_audio = None
    s._turn_revision = 0
    s._nemo_ws = None
    s._sent: list = []
    s._schedule_send = lambda msg: s._sent.append(msg)
    return s


def test_idle_gated_uplink_feeds_vad_but_starts_no_turn():
    import base64

    s = _session()
    s.gate_on_wake = True
    s.pipeline.tts.is_speaking = False
    s._client_playing = False
    s._echo_until = 0.0
    s.echo_tail_ms = 0
    s.echo_max_ms = 12000
    s._echo_logged = False
    s._ns_held = False
    s._echo_hold_started = 0.0
    s._uplink_frames = 0
    s._uplink_log_at = 0.0
    s.ns = None
    s.barge_in_while_speaking = True
    s.pvad = SimpleNamespace(available=False, feed_pcm16=lambda pcm: False)

    class _Vad:
        def __init__(self):
            self.fed = 0

        def feed(self, pcm):
            self.fed += 1
            return [("speech_started", None)]

        def reset(self):
            return None

    s.vad = _Vad()
    s.wake = SimpleNamespace(enabled=True, process=lambda pcm, **kwargs: False, last_score=0.0, barge_threshold=0.10)
    pcm = np.zeros(960, dtype=np.int16).tobytes()
    msg = {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }

    async def _go():
        await s._handle_client(msg)

    asyncio.run(_go())
    assert s.fsm.state == SessionState.IDLE
    assert s.vad.fed == 1
    assert s._sent == []


def test_wake_from_idle_enters_listening():
    s = _session()
    s.pipeline.tts.is_speaking = False
    s._client_playing = False
    s.wake = SimpleNamespace(last_score=0.3)
    s.vad = SimpleNamespace(reset=lambda: None)
    s._on_wake_hit()
    assert s.fsm.state == SessionState.LISTENING
    wake = next(m for m in s._sent if m.get("type") == "wake.detected")
    assert wake.get("score") == 0.3


def test_barge_in_queues_while_lock_held_and_survives_overlap_expiry():
    s = _session()
    s.fsm.begin_turn()
    s.fsm.interrupt("vad_speech_started")
    assert s.fsm.state == SessionState.INTERRUPTING
    audio = np.zeros(1600, dtype=np.float32)
    assert s._turn_lock.acquire(blocking=False)
    s._start_turn(audio, kind="barge_in", speech_stopped_at=1.23)
    assert len(s._pending) == 1
    item = s._pending[0]
    assert item.kind == "barge_in"
    item.t0 = time.monotonic() - (OVERLAP_STALE_S + 0.1)
    fresh = s._pop_fresh_pending()
    assert fresh is not None
    assert fresh.kind == "barge_in"
    s._turn_lock.release()


def test_overlap_while_speaking_is_dropped():
    s = _session()
    s.fsm.begin_turn()
    gid = s.fsm.current.generation_id
    s.fsm.set_thinking(gid)
    s.fsm.set_speaking(gid)
    audio = np.zeros(1600, dtype=np.float32)
    assert s._turn_lock.acquire(blocking=False)
    s._start_turn(audio, kind="overlap")
    assert len(s._pending) == 0
    s._turn_lock.release()


def test_overlap_pending_expires_at_035s():
    s = _session()
    audio = np.zeros(800, dtype=np.float32)
    s._pending.append(
        PendingUtterance(
            audio=audio,
            nemo_result=None,
            t0=time.monotonic() - (OVERLAP_STALE_S + 0.05),
            kind="overlap",
        )
    )
    assert s._pop_fresh_pending() is None


def test_barge_in_pending_expires_only_at_wait_cap():
    s = _session()
    audio = np.zeros(800, dtype=np.float32)
    s._pending.append(
        PendingUtterance(
            audio=audio,
            nemo_result=None,
            t0=time.monotonic() - (BARGE_IN_WAIT_S + 0.05),
            kind="barge_in",
        )
    )
    assert s._pop_fresh_pending() is None


def test_barge_in_sends_playback_cancel():
    s = _session()
    s.fsm.begin_turn()
    s._barge_in("vad_speech_started", abort_nemo=False)
    types = [m.get("type") for m in s._sent]
    assert "playback.cancel" in types
    assert "response.done" in types
    cancel = next(m for m in s._sent if m.get("type") == "playback.cancel")
    assert cancel.get("reason") == "vad_speech_started"


def test_wake_word_while_audible_barges():
    s = _session()
    s.fsm.begin_turn()
    gid = s.fsm.current.generation_id
    s.fsm.set_thinking(gid)
    s.fsm.set_speaking(gid)
    s.pipeline.tts.is_speaking = True
    s._client_playing = True
    s.wake = SimpleNamespace(last_score=0.42)
    s.vad = SimpleNamespace(reset=lambda: None)
    s._on_wake_hit()
    types = [m.get("type") for m in s._sent]
    assert "playback.cancel" in types
    cancel = next(m for m in s._sent if m.get("type") == "playback.cancel")
    assert cancel.get("reason") == "wake_word"
    wake = next(m for m in s._sent if m.get("type") == "wake.detected")
    assert wake.get("score") == 0.42
    assert s.fsm.state == SessionState.LISTENING


def test_reopen_cancel_does_not_start_turn():
    async def _go():
        s = _session()
        s.reopen_ms = 400
        started: list = []
        s._start_turn = lambda *a, **k: started.append(1)

        async def finish(**kw):
            return None

        s._finish_nemo_stream = finish
        s.fsm.state = SessionState.LISTENING
        await s._schedule_utterance_commit(
            np.zeros(800, dtype=np.float32),
            audio_sec=0.05,
            speech_stopped_at=1.0,
        )
        assert s._reopen_task is not None
        s._cancel_reopen()
        await asyncio.sleep(0.05)
        assert started == []

    asyncio.run(_go())


def test_commit_after_reopen_delay_zero_starts_turn_with_barge_kind():
    async def _go():
        s = _session()
        s.reopen_ms = 0
        started: list = []
        s._start_turn = lambda *a, **k: started.append(k)

        async def finish(**kw):
            return {"text": "hi"}

        s._finish_nemo_stream = finish
        s.fsm.begin_listen()
        s.fsm.interrupt("vad")
        await s._schedule_utterance_commit(
            np.zeros(800, dtype=np.float32),
            audio_sec=0.05,
            speech_stopped_at=2.5,
        )
        assert started
        assert started[0].get("kind") == "barge_in"
        assert started[0].get("speech_stopped_at") == 2.5

    asyncio.run(_go())


def test_nemo_reuse_does_not_abort_open_socket():
    async def _go():
        s = _session()
        marker = object()
        s._nemo_ws = marker
        aborted: list = []

        async def abort():
            aborted.append(True)
            s._nemo_ws = None

        s._abort_nemo_stream = abort
        await s._start_nemo_stream(reuse=True)
        assert s._nemo_ws is marker
        assert aborted == []
        await s._start_nemo_stream(reuse=False)
        assert aborted == [True]

    asyncio.run(_go())


def test_agent_loop_skips_tools_after_abort(monkeypatch):
    called: list = []

    def boom(*_a, **_k):
        called.append(True)
        return {}

    monkeypatch.setattr("nova_hailo.pipeline.execute_tool", boom)
    stub = SimpleNamespace(
        tools_enabled=True,
        abort_event=threading.Event(),
        _barge_in_flag=False,
        multi_tool_rounds=3,
        llm=MagicMock(),
        tts=MagicMock(),
        wait_tts_drain=False,
        tools=None,
    )
    inf = SimpleNamespace(to_dict=lambda: {}, ttft_ms=None, first_token_time=None)
    stub.llm.generate.return_value = ("raw", inf)
    stub.llm.parse_tool.return_value = {
        "name": "web_search",
        "arguments": {"query": "x"},
    }
    stub._with_genai = lambda fn: fn()
    stub._messages = lambda *a, **k: []
    stub._turn_cancelled = lambda: NovaPipeline._turn_cancelled(stub)
    stub.abort_event.set()
    speak, tool, _bundle = NovaPipeline._agent_loop(
        stub, "hi", True, time.perf_counter(), TurnMetrics()
    )
    assert speak == ""
    assert tool is None
    assert called == []
    stub.llm.parse_tool.assert_not_called()


def test_agent_loop_skips_execute_tool_if_abort_during_parse(monkeypatch):
    called: list = []
    monkeypatch.setattr(
        "nova_hailo.pipeline.execute_tool",
        lambda *_a, **_k: called.append(True) or {},
    )
    stub = SimpleNamespace(
        tools_enabled=True,
        abort_event=threading.Event(),
        _barge_in_flag=False,
        multi_tool_rounds=3,
        llm=MagicMock(),
        tts=MagicMock(),
        wait_tts_drain=False,
        tools=None,
    )
    inf = SimpleNamespace(to_dict=lambda: {}, ttft_ms=None, first_token_time=None)
    stub.llm.generate.return_value = ("raw", inf)

    def parse(_raw):
        stub.abort_event.set()
        return {"name": "web_search", "arguments": {"query": "x"}}

    stub.llm.parse_tool.side_effect = parse
    stub._with_genai = lambda fn: fn()
    stub._messages = lambda *a, **k: []
    stub._turn_cancelled = lambda: NovaPipeline._turn_cancelled(stub)
    NovaPipeline._agent_loop(stub, "hi", True, time.perf_counter(), TurnMetrics())
    assert called == []


def test_openrouter_abort_closes_http_stream():
    closed = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        def gen():
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        resp = httpx.Response(
            200,
            content=gen(),
            headers={"Content-Type": "text/event-stream"},
        )
        orig = resp.close

        def close():
            closed["n"] += 1
            orig()

        resp.close = close
        return resp

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenRouterLLM(api_key="sk-test", model="v4-flash", client=client)
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return True

    llm.generate([{"role": "user", "content": "hi"}], abort_callback=abort)
    assert llm.last_closed_on_abort is True
    assert closed["n"] >= 1


def test_retry_during_thinking_does_not_interrupt():
    """09:03 soak: STT then INTERRUPTING — user repeat aborted the first turn."""
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.THINKING,
            tts_speaking=False,
            barge_in_while_speaking=True,
        )
        is False
    )
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.TRANSCRIBING,
            tts_speaking=False,
            barge_in_while_speaking=True,
        )
        is False
    )


def test_barge_only_while_tts_audible_and_enabled():
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.SPEAKING,
            tts_speaking=True,
            barge_in_while_speaking=True,
        )
        is True
    )
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.SPEAKING,
            tts_speaking=True,
            barge_in_while_speaking=False,
        )
        is False
    )
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.LISTENING,
            tts_speaking=False,
            barge_in_while_speaking=True,
        )
        is False
    )
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.LISTENING,
            tts_speaking=False,
            barge_in_while_speaking=True,
            client_playing=True,
        )
        is True
    )
    assert (
        should_interrupt_on_user_speech(
            fsm_state=SessionState.INTERRUPTING,
            tts_speaking=True,
            barge_in_while_speaking=True,
        )
        is False
    )


def test_transcript_is_echo_catches_live_session_garble():
    from nova_hailo.web.realtime_session import _transcript_is_echo

    assert _transcript_is_echo("It's Tense.", "It's 10:09 am.") is True
    assert (
        _transcript_is_echo(
            "I'm not sure if it's exact time right now.",
            "I'm not sure of the exact time right now.",
        )
        is True
    )
    assert _transcript_is_echo("are you listening or not", "It's 10:09 am.") is False
    assert _transcript_is_echo("What is the current time now?", "") is False


def test_playback_stopped_keeps_echo_window():
    from nova_hailo.web.realtime_session import apply_playback_stopped

    out = apply_playback_stopped(
        now=10.0, echo_until=9.5, echo_tail_s=0.4, hold_s=1.8
    )
    assert out["release_speaking"] is False
    assert out["client_playing"] is False
    assert out["echo_until"] >= 10.0 + 0.4 + 1.8


def test_playback_interrupted_clears_echo_window():
    from nova_hailo.web.realtime_session import apply_playback_interrupted

    out = apply_playback_interrupted()
    assert out["release_speaking"] is True
    assert out["client_playing"] is False
    assert out["echo_until"] == 0.0


def test_turn_completion_extends_the_followup_window():
    """session_idle_sec is an *idle* timeout, not a session lifetime.

    _armed_until is read two ways: it expires the ARMED state, and it decides
    whether a finished turn lands in LISTENING or IDLE. Only the first is a
    deadline from the wake word; the second has to mean "time since the user
    last did something", or a conversation outliving the window drops to IDLE
    mid-flow and silently demands a new wake word.
    """
    from nova_hailo.session_fsm import TurnTerminal

    fsm = SessionFSM(session_idle_sec=0.05)
    fsm.arm()
    ctx = fsm.begin_turn()
    time.sleep(0.08)  # the window opened by arm() has now elapsed
    assert fsm.complete(ctx.generation_id, TurnTerminal.COMPLETED) is True
    assert fsm.state is SessionState.LISTENING


def test_barge_in_extends_the_followup_window():
    """Interrupting is the strongest evidence of an engaged user there is.

    complete() and release_speaking() are the other two terminal paths and
    both refresh the window; interrupt() refreshing nothing is what strands a
    barge-in followed by a dropped utterance in IDLE.
    """
    from nova_hailo.session_fsm import TurnTerminal

    fsm = SessionFSM(session_idle_sec=0.05)
    fsm.arm()
    first = fsm.begin_turn()
    fsm.set_speaking(first.generation_id)
    time.sleep(0.08)
    fsm.interrupt(reason="vad_speech_started")

    # The user is mid-sentence; a turn committed now is a follow-up, not a
    # cold start that should need the wake word again.
    ctx = fsm.begin_turn()
    assert fsm.complete(ctx.generation_id, TurnTerminal.COMPLETED) is True
    assert fsm.state is SessionState.LISTENING


def test_topical_follow_up_is_not_mistaken_for_echo():
    # A follow-up that reuses the last answer's topic words was dropped as
    # Nova's own voice ("skip TTS-echo"), so the user got silence.
    from nova_hailo.web.realtime_session import _transcript_is_echo

    assert _transcript_is_echo(
        "What is the current stock price of NVIDIA?",
        "I'm not able to look up real-time market data, so I can't give you the "
        "current SpaceX stock price.",
    ) is False
    # A real follow-up and the reply it followed.
    assert _transcript_is_echo(
        "Where is it that landslides and waterlogging happened?",
        "Um, today's broad news in India includes severe rain causing landslides, "
        "water-logging and road disruptions across several states, and a Supreme "
        "Court order upholding a decision that quashed a student's detention under.",
    ) is False


def test_real_echoes_are_still_caught():
    from nova_hailo.web.realtime_session import _transcript_is_echo

    # A real echo: the tail of Nova's reply, heard back by the mic.
    assert _transcript_is_echo(
        "Yeah, that's the most recent version from SpaceX AI.",
        "Hmm, it looks like the latest Grok release is actually Grok 4. announced "
        "around September 21 2026. Yeah, that's the most recent version from SpaceX AI.",
    ) is True
    # Garbled but in order.
    assert _transcript_is_echo(
        "it's warm in deli today around thirty three degrees in the afternoon",
        "Sure, it's warm in Delhi today, around 33 degrees in the afternoon with a "
        "light chance of rain.",
    ) is True


import pytest  # noqa: E402


@pytest.mark.parametrize("aec,trust,server,allowed", [
    (None, False, False, False),   # no claim: the server's setting decides
    (None, False, True, True),
    (True, False, False, False),   # an untrusted "I cancel echo" no longer opens the gate
    (True, False, True, True),
    (True, True, False, True),     # trusted, as before
    (False, False, True, False),   # "no canceller" always closes it
    (False, True, True, False),
])
def test_barge_policy_matrix(aec, trust, server, allowed):
    s = _session()
    s._client_aec, s.trust_client_aec, s.barge_in_while_speaking = aec, trust, server
    assert s._barge_allowed() is allowed


def test_with_the_board_cancelling_echo_only_speech_over_nova_is_suspect():
    """"It's Lucy Stern Community Center." right after Nova said "Navigating to
    Lucie Stern Community Center" was dropped as echo. With the XVF3800 trusted, speech that
    starts after Nova stopped is the driver."""
    s = _session()
    s._client_aec, s.trust_client_aec, s.echo_suspect_s = True, True, 1.5
    s._last_audible_mono, s._last_barge_mono = 100.0, 0.0
    assert s._echo_suspect(100.1) is True      # began while she was audible
    assert s._echo_suspect(100.8) is False     # began after she stopped
    s.trust_client_aec = False                 # no trusted canceller: the old 1.5 s window
    assert s._echo_suspect(100.8) is True


@pytest.mark.parametrize("hw,state,audio_s,yes", [
    (True, "THINKING", 0.9, True),        # a correction while Nova thinks replaces the turn
    (True, "TRANSCRIBING", 0.6, True),
    (True, "THINKING", 0.3, False),       # a quick "okay" does not cancel anything
    (False, "THINKING", 0.9, False),      # no trusted canceller: may be her echo backlog
    (True, "SPEAKING", 0.9, False),       # while she is audible, barge-in already handles it
])
def test_speech_during_thinking_supersedes_the_turn(hw, state, audio_s, yes):
    from nova_hailo.session_fsm import SessionState
    from nova_hailo.web.realtime_session import supersedes
    assert supersedes(hw_aec=hw, state=SessionState(state), audio_s=audio_s) is yes


@pytest.mark.parametrize("prefix,text,out", [
    ("Find theaters with Dolby Atmos.", "No, no, the ones showing Jailer 2.",
     "Find theaters with Dolby Atmos. No, no, the ones showing Jailer 2."),
    ("", "Stop the navigation.", "Stop the navigation."),
    ("Take me to Kirk's", "Take me to Kirk's Steakburgers", "Take me to Kirk's Steakburgers"),
])
def test_a_superseding_utterance_keeps_the_words_it_replaced(prefix, text, out):
    from nova_hailo.web.realtime_session import fold_superseded
    assert fold_superseded(prefix, text) == out


@pytest.mark.parametrize("guard,text,suspect,hw,answering,drop", [
    (True, False, False, True, False, False),   # board trusted, spoke after Nova: keep the correction
    (True, False, True, True, False, True),     # board trusted, spoke over Nova: her words, drop
    (True, False, False, False, False, True),   # no trusted canceller: the word-pair guard still drops
    (False, True, True, False, False, True),
    (True, True, True, True, True, False),      # a yes or no to a pending question is an answer
    (False, False, True, True, False, False),
])
def test_echo_drop_decision(guard, text, suspect, hw, answering, drop):
    from nova_hailo.web.realtime_session import echo_drop
    assert echo_drop(guard_hit=guard, text_hit=text, suspect=suspect, hw_aec=hw, answering=answering) is drop


@pytest.mark.parametrize("env,cfg,hw,trusted", [
    ("", False, "", False),          # nothing says the claim is real
    ("", True, "", True),            # voice.trust_client_aec
    ("", False, "xvf3800", True),    # this Pi's mic is a board that cancels echo itself
    ("0", False, "xvf3800", False),  # the env switch still wins, both ways
    ("1", False, "", True),
    ("", False, "0", False),
])
def test_a_hardware_canceller_on_this_pi_makes_the_client_claim_trusted(env, cfg, hw, trusted):
    from nova_hailo.web.realtime_session import client_aec_trusted
    assert client_aec_trusted(env, cfg, hw) is trusted


def test_the_session_logs_what_the_board_rules_would_do(monkeypatch, capsys):
    """Log-only wiring in RealtimeSession: no behaviour change, just [turn] / [xvf] lines."""
    import nova_hailo.web.realtime_session as rs
    from nova_hailo.xvf3800 import BoardState

    states = iter([BoardState(t, (0, 0, 0, e), True, False) for t, e in [(0, 0)] * 5 + [(0, 9e5)] * 40 + [(0, 0)] * 30])

    class _Board:
        def latest(self, max_age_s=0.25):
            return next(states, None)

    monkeypatch.setattr(rs.xvf3800, "board", lambda: _Board())
    monkeypatch.setenv("NOVA_HAILO_XVF_TURNS", "log")
    s = rs.RealtimeSession.__new__(rs.RealtimeSession)
    s._client_aec, s.trust_client_aec = True, True
    s._speech_started_mono = None
    clock = [100.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock[0])
    for i in range(75):
        s._speech_started_mono = clock[0] if 5 <= i < 45 else None
        s._xvf_frame(speaking=i < 70)
        clock[0] += 0.02
    out = capsys.readouterr().out
    assert "[turn] speech over Nova" in out
    assert "[turn] would barge" in out
    assert "[xvf] Nova spoke" in out


def test_the_board_hook_works_before_any_vad_event_and_never_breaks_the_audio_loop(monkeypatch, capsys):
    """The first frame read _speech_started_mono before the VAD had set it; the
    AttributeError closed the WebSocket and the client reconnected in a loop."""
    import nova_hailo.web.realtime_session as rs
    from nova_hailo.xvf3800 import BoardState

    class _Board:
        def latest(self, max_age_s=0.25):
            return BoardState(0.0, (0, 0, 0, 0.0), True, False)

    monkeypatch.setattr(rs.xvf3800, "board", lambda: _Board())
    s = rs.RealtimeSession.__new__(rs.RealtimeSession)
    s._client_aec, s.trust_client_aec = True, True
    s._xvf_frame(speaking=True)            # no _speech_started_mono yet

    class _Broken:
        def latest(self, max_age_s=0.25):
            raise RuntimeError("usb went away")

    monkeypatch.setattr(rs.xvf3800, "board", lambda: _Broken())
    s._xvf_frame(speaking=True)            # logs, does not raise
    s._xvf_frame(speaking=True)
    out = capsys.readouterr().out
    assert out.count("[xvf] turn logging stopped") == 1


# ---- V2b: duck at the first syllable, then decide ----------------------------------------

def _duck_session(monkeypatch, board_state=None):
    import nova_hailo.web.realtime_session as rs
    from nova_hailo.web.turn_taking import Duck

    class _Board:
        def latest(self, max_age_s=0.25):
            return board_state

    monkeypatch.setattr(rs.xvf3800, "board", lambda: _Board())
    monkeypatch.setenv("NOVA_HAILO_XVF_TURNS", "on")
    s = rs.RealtimeSession.__new__(rs.RealtimeSession)
    s._client_aec, s.trust_client_aec = True, True
    s.sent, s.barges = [], []
    s._schedule_send = lambda ev, mirror=True: s.sent.append(ev)
    s._barge_in = lambda reason, abort_nemo=True: s.barges.append(reason)
    s._call_on_loop = lambda fn, *a: fn(*a)
    s._pending = collections.deque(maxlen=4)
    s._duck_state = Duck()
    return s


def test_ducking_is_allowed_only_with_a_trusted_settled_board_while_nova_speaks(monkeypatch):
    from nova_hailo.xvf3800 import BoardState

    s = _duck_session(monkeypatch, BoardState(0.0, (0, 0, 0, 0), True, False))
    assert s._duck_allowed(speaking=True) is True
    assert s._duck_allowed(speaking=False) is False
    s = _duck_session(monkeypatch, BoardState(0.0, (0, 0, 0, 0), False, False))    # canceller not settled
    assert s._duck_allowed(speaking=True) is False
    monkeypatch.setenv("NOVA_HAILO_XVF_TURNS", "log")
    s = _duck_session(monkeypatch, BoardState(0.0, (0, 0, 0, 0), True, False))
    monkeypatch.setenv("NOVA_HAILO_XVF_TURNS", "log")
    assert s._duck_allowed(speaking=True) is False


def _item():
    import numpy as np
    import nova_hailo.web.realtime_session as rs

    return rs.PendingUtterance(audio=np.zeros(8000, dtype=np.float32), nemo_result=None, t0=0.0)


def test_mm_hm_over_nova_brings_her_volume_back_and_is_not_a_turn(monkeypatch):
    s = _duck_session(monkeypatch)
    s._duck_state.start(0.0)
    s._side_transcript = lambda audio: "Mm-hm."
    s._judge_short(_item())
    assert s.sent == [{"type": "output_audio.duck", "gain": 1.0}]
    assert not s._pending and not s.barges


def test_stop_over_nova_stops_her_and_becomes_the_turn_without_a_second_transcription(monkeypatch):
    s = _duck_session(monkeypatch)
    s._duck_state.start(0.0)
    s._side_transcript = lambda audio: "Stop."
    s._judge_short(_item())
    assert s.barges == ["short_stop"]
    item = s._pending[0]
    assert item.kind == "barge_in" and item.nemo_result["text"] == "Stop."
    # The turn waits on the sidecar event before reading the text: it must already be set.
    assert item.nemo_result["event"].is_set() and item.nemo_result["audio_sec"] == 0.5


def test_talking_on_past_the_hold_stops_nova(monkeypatch):
    import nova_hailo.web.realtime_session as rs

    s = _duck_session(monkeypatch)
    clock = [50.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock[0])
    s._duck_state.start(50.0)
    s._speech_started_mono = 50.0
    clock[0] = 50.3
    s._duck_tick(speaking=True)
    assert not s.barges
    clock[0] = 50.55
    s._duck_tick(speaking=True)
    assert s.barges == ["sustained_speech"]


def test_nova_finishing_while_ducked_brings_the_volume_back(monkeypatch):
    import nova_hailo.web.realtime_session as rs

    s = _duck_session(monkeypatch)
    monkeypatch.setattr(rs.time, "monotonic", lambda: 10.0)
    s._duck_state.start(9.9)
    s._duck_tick(speaking=False)
    assert s.sent == [{"type": "output_audio.duck", "gain": 1.0}]


def test_the_duck_counts_the_vads_voiced_frames(monkeypatch):
    import nova_hailo.web.realtime_session as rs
    from types import SimpleNamespace as NS

    s = _duck_session(monkeypatch)
    s.vad = NS(last_prob=0.9, cfg=NS(threshold=0.55))
    s._speech_started_mono = 1.0
    clock = [1.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock[0])
    s._duck_state.start(1.0)
    for i in range(40):                      # 0.3 s voiced, then hangover with low probability
        clock[0] += 0.02
        s.vad.last_prob = 0.9 if i < 15 else 0.1
        s._duck_tick(speaking=True)
    assert not s.barges and s._duck_state.active


def test_a_sound_too_short_to_judge_brings_novas_volume_back_at_once(monkeypatch):
    s = _duck_session(monkeypatch)
    s._duck_state.start(0.0)
    s._duck_dropped()
    assert s.sent == [{"type": "output_audio.duck", "gain": 1.0}] and not s._duck_state.active
    s._duck_dropped()                        # nothing ducked: nothing sent
    assert len(s.sent) == 1
