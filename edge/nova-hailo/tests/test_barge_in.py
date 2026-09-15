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
