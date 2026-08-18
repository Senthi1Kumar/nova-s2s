"""Host controller + vui-like helpers (state, memory, barge-in, speak).

Callers: pytest. Exercises shipped edge_harness / oem_tools / pipeline hooks
without Hailo hardware.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nova_hailo.edge_harness.controller import (
    allows_free_chat_generate,
    decide_turn,
    gate_after_router,
    host_turn,
    is_thin_utterance,
)
from nova_hailo.edge_harness.interrupt import GenerationGuard
from nova_hailo.edge_harness.memory_store import STARTUP_GREET_PREFIX, MemoryStore
from nova_hailo.edge_harness.router import CLARIFY_SPEAK
from nova_hailo.edge_harness.speak_sanitize import has_filler, sanitize_for_tts
from nova_hailo.edge_harness.task_state import (
    TaskState,
    apply_routed_turn,
    is_same_task,
    record_clarify,
)
from nova_hailo.tools.oem_tools import OemToolGateway
from nova_hailo.web.fail_closed import FAIL_CLOSED_SPEAK

ROOT = Path(__file__).resolve().parents[1]


def _gw_with_search(monkeypatch):
    gw = OemToolGateway(enabled=["web_search", "deep_research", "check_calendar"])

    def fake_execute(routed):
        return {
            "ok": True,
            "name": routed.intent.value,
            "status": "success",
            "speak": f"ok:{routed.query}",
            "result": {"q": routed.query},
        }

    monkeypatch.setattr(gw._broker, "execute", fake_execute)
    return gw


def test_search_again_and_coref_use_host_task_state(monkeypatch):
    gw = _gw_with_search(monkeypatch)
    first = host_turn(gw, "search the web for current prime minister of the UK")
    assert first["path"] == "tool" and first["invoke_llm"] is False
    assert gw.task_state.last_tool == "web_search"
    assert gw.task_state.last_query
    first_q = gw.task_state.last_query
    assert gw.task_state.completed is True

    again = host_turn(gw, "search again")
    assert again["path"] == "tool" and again["invoke_llm"] is False
    assert again["tool_out"]["speak"] == f"ok:{first_q}"
    assert gw.task_state.last_query == first_q
    assert again["clear_llm"] is False  # same task — keep native context

    coref = host_turn(gw, "NVIDIA only")
    assert coref["path"] == "tool" and coref["invoke_llm"] is False
    assert gw.task_state.intent == "web_search"
    assert "nvidia" in (gw.task_state.last_query or "").lower()


def test_new_intent_after_completed_task_requests_clear(monkeypatch):
    gw = _gw_with_search(monkeypatch)
    host_turn(gw, "search the web for hailo")
    assert gw.task_state.completed is True
    cal = host_turn(gw, "what meetings do I have today?")
    assert cal["path"] == "tool"
    assert gw.task_state.intent == "check_calendar"
    assert cal["clear_llm"] is True


def test_apply_routed_turn_same_task_keeps_kv():
    prev = TaskState(intent="web_search", last_tool="web_search", last_query="hailo")
    nxt, clear = apply_routed_turn(
        prev,
        user_text="search again",
        new_intent="web_search",
        search_again=True,
        tool_ok=True,
        compact_result="ok",
    )
    assert is_same_task(prev, "web_search", search_again=True)
    assert clear is False
    assert nxt.last_query == "hailo"


def test_unmatched_empty_thin_never_invoke_free_chat(monkeypatch):
    invoked = []

    def generate_stub(*_a, **_k):
        invoked.append(True)
        raise AssertionError("free-chat generate must not run")

    empty = decide_turn("", TaskState(), routed_intent=None)
    thin = decide_turn("uh", TaskState(), routed_intent=None)
    unmatched = decide_turn("that's a long drive isn't it", TaskState(), routed_intent=None)
    assert empty.path == "empty" and empty.speak == FAIL_CLOSED_SPEAK
    assert thin.path == "thin" and thin.speak == FAIL_CLOSED_SPEAK
    assert unmatched.path == "clarify" and unmatched.speak == CLARIFY_SPEAK
    for d in (empty, thin, unmatched):
        assert d.invoke_llm is False
        assert allows_free_chat_generate(d) is False
        if allows_free_chat_generate(d):
            generate_stub()
    assert invoked == []
    assert is_thin_utterance("uh")
    assert is_thin_utterance("mm")
    assert not is_thin_utterance("hello")

    gw = _gw_with_search(monkeypatch)
    gated = host_turn(gw, "that's a long drive isn't it")
    assert gated["path"] == "clarify"
    assert gated["invoke_llm"] is False
    assert gated["tool_out"] is None
    assert gated["speak"] == CLARIFY_SPEAK

    joke = host_turn(gw, "tell me a joke about cars")
    assert joke["invoke_llm"] is True
    assert joke["path"] == "chat"

    # Second unmatched after a clarify must not loop the same line.
    gw.task_state = record_clarify(gw.task_state)
    second = decide_turn("with you", gw.task_state, routed_intent=None)
    assert second.invoke_llm is True
    assert invoked == []


def test_coffee_and_capability_do_not_clarify_loop(monkeypatch):
    from nova_hailo.edge_harness.router import route
    from nova_hailo.edge_harness.policy import CapabilityProfile
    from nova_hailo.edge_harness.types import Intent

    profile = CapabilityProfile.from_list(["web_search"])
    for utt in (
        "Coffee shops near in Las Ghettos",
        "Best coffee shops or restaurants in Los Gh",
        "Find some uh best coffee shops in near in Las",
        "Some good coffee shops near in Los Gietos, California",
        "Some best coffee shops in Las Guetos",
    ):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.WEB_SEARCH, utt

    gw = _gw_with_search(monkeypatch)
    out = host_turn(gw, "Best coffee shops in Los Gatos")
    assert out["path"] == "tool"
    assert out["invoke_llm"] is False
    assert out["tool_out"] and out["tool_out"]["ok"]

    assert gw.route("Can you do?") == "capability"
    cap = host_turn(gw, "Can you do?")
    assert cap["invoke_llm"] is False
    assert cap["path"] == "tool"
    assert cap["speak"]
    assert cap["speak"] != CLARIFY_SPEAK


def test_memory_persist_reload_startup_greet(tmp_path):
    path = tmp_path / "nova_memory.json"
    store = MemoryStore(path)
    store.add("web_search: current prime minister of the UK")
    reloaded = MemoryStore(path)
    fact = reloaded.last_session_fact()
    assert fact and "prime minister" in fact
    greet = reloaded.startup_greet()
    assert greet.startswith(STARTUP_GREET_PREFIX)
    assert "prime minister" in greet
    assert "\n" not in greet
    assert has_filler(greet)


def test_generation_guard_invalidates_pcm():
    g = GenerationGuard()
    gid = g.begin()
    assert g.should_enqueue_pcm(gid) is True
    g.invalidate("barge_in")
    assert g.should_enqueue_pcm(gid) is False
    assert g.is_live(gid) is False
    nxt = g.begin()
    assert nxt != gid
    assert g.should_enqueue_pcm(gid) is False
    assert g.should_enqueue_pcm(nxt) is True


def test_sanitize_strips_json_keeps_fillers():
    spoken = sanitize_for_tts('Yeah, um, the kitchen light is on. {"type":"call","tool":"set_light"}')
    assert "Yeah" in spoken or "yeah" in spoken.lower()
    assert "um" in spoken.lower()
    assert "{" not in spoken
    assert "set_light" not in spoken
    assert has_filler(spoken)
    dumped = sanitize_for_tts('{"type":"call","tool":"web_search","args":{}}')
    assert "{" not in dumped
    assert dumped == "I'm here." or dumped[0].isalpha()


def test_enable_in_prompt_stays_false():
    for name in ("config.oem_v002_test.yaml", "config.oem.yaml", "config.yaml"):
        raw = yaml.safe_load((ROOT / name).read_text())
        assert raw["tools"]["enable_in_prompt"] is False, name


def test_pipeline_and_session_wire_controller():
    src = (ROOT / "nova_hailo" / "pipeline.py").read_text()
    assert "gate_after_router" in src
    assert "task_boundary_clear" in src
    assert "sanitize_for_tts" in src
    assert "gen_guard" in src
    assert "should_enqueue_pcm" in src
    rs = (ROOT / "nova_hailo" / "web" / "realtime_session.py").read_text()
    assert "greeting" in rs
    assert "gen_guard" in rs
    assert "_barge_in" in rs
    assert "response.cancel" in rs


def test_soul_allows_conversational_fillers():
    soul = (ROOT / "prompts" / "soul.md").read_text()
    assert "um" in soul.lower()
    assert "yeah" in soul.lower()


def test_ws_loop_stays_realtime_cascade():
    """Mic PCM → VAD → STT → host controller → TTS PCM (no Vui GPU stack)."""
    rs = (ROOT / "nova_hailo" / "web" / "realtime_session.py").read_text()
    assert "input_audio_buffer.append" in rs
    assert "speech_stopped" in rs
    assert "run_text_turn" in rs
    assert "response.audio.delta" in rs
    assert "cloned/vui" not in rs
    pipe = (ROOT / "nova_hailo" / "pipeline.py").read_text()
    assert "gate_after_router" in pipe
    assert "sanitize_for_tts" in pipe
