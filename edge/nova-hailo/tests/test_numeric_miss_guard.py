"""After a price lookup finds nothing, streamed speech must not invent the price.

The miss goes to the cloud model's second round so follow-ups ("Why is it?")
get a real answer; this pins the clause-level guard that keeps that round
from voicing a number the search never returned. No Hailo, no network.
"""
from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace

import pytest
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

from nova_hailo.edge_harness.interrupt import GenerationGuard  # noqa: E402
from nova_hailo.pipeline import NovaPipeline  # noqa: E402


def _pipeline():
    p = NovaPipeline.__new__(NovaPipeline)
    p.abort_event = threading.Event()
    p.gen_guard = GenerationGuard()
    gid = p.gen_guard.begin()
    queued: list[str] = []
    p.tts = SimpleNamespace(
        enabled=True,
        get_current_gen_id=lambda: gid,
        queue_text=lambda text, g: queued.append(text),
    )
    p.validate_before_speak = False
    p.on_validated_clause = None
    p.on_token = None
    p.stream_tts = True
    return p, gid, queued


def _state(**extra):
    st = {"sentence_buffer": "", "spoken_parts": [], "first_chunk_sent": False}
    st.update(extra)
    return st


def test_clause_with_invented_price_is_dropped_after_miss():
    p, gid, queued = _pipeline()
    st = _state(forbid_amounts=True)
    p._emit_validated_clause("NVIDIA is trading around $130 right now.", gid, st)
    assert queued == []
    assert st["spoken_parts"] == []


def test_honest_clause_is_spoken_after_miss():
    p, gid, queued = _pipeline()
    st = _state(forbid_amounts=True)
    p._emit_validated_clause("I couldn't find a live price for NVIDIA.", gid, st)
    assert queued == ["I couldn't find a live price for NVIDIA."]


def test_amounts_are_spoken_normally_without_a_miss():
    p, gid, queued = _pipeline()
    p._emit_validated_clause("Search results say NVIDIA $182.40.", gid, _state())
    assert queued == ["Search results say NVIDIA $182.40."]


# ---- A claimed car change with no vehicle tool behind it ----------------------

from nova_hailo.edge_harness.cloud_tool_speak import (  # noqa: E402
    CLAIM_WITHOUT_ACTION_SPEAK,
    asks_for_action,
    claims_vehicle_action,
    vehicle_acted,
)


def test_a_claimed_stop_after_only_a_web_search_is_not_spoken():
    p, gid, queued = _pipeline()
    st = _state(claim_flags={"wrote": False, "warned": False})
    p._emit_validated_clause("Sure, I've added a stop at Don Giovanni on Castro Street.", gid, st)
    p._emit_validated_clause("It's a well-rated Italian spot right along our route.", gid, st)
    p._emit_validated_clause("Navigating to Don Giovanni now.", gid, st)
    assert queued == [CLAIM_WITHOUT_ACTION_SPEAK, "It's a well-rated Italian spot right along our route."]


def test_a_claim_backed_by_a_vehicle_tool_is_spoken():
    p, gid, queued = _pipeline()
    st = _state(claim_flags={"wrote": True, "warned": False})
    p._emit_validated_clause("Added a stop at Don Giovanni.", gid, st)
    assert queued == ["Added a stop at Don Giovanni."]


@pytest.mark.parametrize(("text", "claim"), [
    ("Sure, I've added a stop at Don Giovanni.", True),
    ("Navigating to Palo Alto.", True),
    ("The fan is now set to three.", True),
    ("I can add a stop there if you like.", False),
    ("Do you want me to add it as a stop?", False),
    ("I'm not able to set a reminder for that stop.", False),
])
def test_claims_vehicle_action(text, claim):
    assert claims_vehicle_action(text) is claim


def test_asked_for_a_change_or_about_something():
    assert asks_for_action("Okay, find the best restaurants and add a stop to it.")
    assert not asks_for_action("What's the best restaurant in Mountain View?")


def test_vehicle_acted_counts_a_no_op_success_but_not_a_question():
    assert vehicle_acted({"status": "success", "result": {"diff": []}})
    assert vehicle_acted({"status": "success", "result": {"diff": [{"path": "x"}]}})
    assert not vehicle_acted({"status": "confirm", "result": {"diff": []}})
    assert not vehicle_acted({"status": "clarify"})
    assert not vehicle_acted(None)


# ---- The whole cloud turn: "find a restaurant and add a stop" -----------------

from nova_hailo.cabingym.devices import build_devices  # noqa: E402
from nova_hailo.context_history import ConversationHistory  # noqa: E402
from nova_hailo.edge_harness.policy import CapabilityProfile  # noqa: E402
from nova_hailo.metrics import TurnMetrics  # noqa: E402

_SEARCH = {"name": "web_search", "arguments": {"query": "best restaurant Mountain View"}}
_ADD = {"name": "vehicle_navigation", "arguments": {"action": "add_stop", "destination": "Don Giovanni"}}


class _ScriptedLLM:
    """Round by round: (tool calls, streamed text). Records what each round was offered."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.offered = []
        self.last_tool_calls = []

    def generate(self, messages, tool_choice="auto", tools=None, token_callback=None, **kw):
        self.offered.append((tool_choice, sorted(t["function"]["name"] for t in tools or [])))
        calls, text = self.rounds.pop(0) if self.rounds else ([], "")
        self.last_tool_calls = calls
        if text and token_callback:
            token_callback(text)
        return text, None


class _Gateway:
    def __init__(self, add_stop_status):
        self.profile = CapabilityProfile.from_list(["web_search", "vehicle"])
        self.cabin = SimpleNamespace(devices=build_devices(), dialog=[])
        self.task_state = None
        self.executed = []
        self.add_stop_status = add_stop_status

    def execute(self, name, **kw):
        self.executed.append(name)
        if name == "web_search":
            return {"ok": True, "name": name, "status": "success",
                    "speak": "Don Giovanni on Castro Street is well rated.", "result": {}}
        return {"ok": True, "name": "vehicle", "status": self.add_stop_status,
                "speak": "Added a stop at Don Giovanni.", "result": {"diff": [{"path": "stops"}]}}

    def maybe_rescue_vehicle(self, user_text, name, out):
        return out

    def take_cabin_turn(self, text, dialog=None):
        return None   # not a direct cabin command: the cloud model picks the tools


def _cloud_pipeline(llm, gateway):
    p, gid, queued = _pipeline()
    p.tts.begin_turn = lambda: gid
    p.tts.wait_drain = lambda timeout=0: None
    p.tts.timing_snapshot = lambda: {}
    p.wait_tts_drain = False
    p.llm, p.llm_backend, p.oem_gateway = llm, "groq", gateway
    p.history = ConversationHistory(max_turns=5)
    p.cfg = SimpleNamespace(soul_prompt="")
    p.multi_tool_rounds, p.exa_direct_answer, p.speak_tool_ack = 3, False, False
    p.voice_one_sentence, p.first_chunk_intra_split, p.first_chunk_min_chars = False, False, 1
    p.turn_trusted, p.research_pending = True, SimpleNamespace(add=lambda *a, **k: None)
    p.active_generation_id = gid
    p._cloud_location_sentence = lambda: ""
    p._cloud_host_route = lambda text: None
    p._with_genai = lambda fn: fn()
    p._turn_cancelled = lambda: False
    p._emit_tool_status = lambda *a, **k: None
    p._emit_agent_trace = lambda *a, **k: None
    p.session = SimpleNamespace(add=lambda m: None)
    p._speak_tool_line = lambda line, **kw: SimpleNamespace(assistant_text=line)
    return p, queued


def _turn(p, text):
    return p._openrouter_turn(text, wall0=0.0, metrics=TurnMetrics(), auth_decision="n/a",
                              record_session=False)


def test_after_a_lookup_the_next_round_may_add_the_stop():
    llm = _ScriptedLLM([([_SEARCH], ""), ([_ADD], ""), ([], "")])
    gw = _Gateway("success")
    p, queued = _cloud_pipeline(llm, gw)
    _turn(p, "Let's add a restaurant stop in Mountain View area.")
    assert gw.executed == ["web_search", "vehicle_navigation"]
    choice, names = llm.offered[1]
    assert choice == "auto" and names and all(n.startswith("vehicle_") for n in names)
    assert "Added a stop at Don Giovanni." in queued


def test_a_claim_after_only_a_lookup_is_replaced_with_the_truth():
    llm = _ScriptedLLM([([_SEARCH], ""), ([], "Sure, I've added a stop at Don Giovanni.")])
    p, queued = _cloud_pipeline(llm, _Gateway("success"))
    _turn(p, "Let's add a restaurant stop in Mountain View area.")
    assert queued == [CLAIM_WITHOUT_ACTION_SPEAK]


def test_a_question_after_a_lookup_gets_no_tools():
    llm = _ScriptedLLM([([_SEARCH], ""), ([], "Don Giovanni is well rated.")])
    p, _ = _cloud_pipeline(llm, _Gateway("success"))
    _turn(p, "What's the best restaurant in Mountain View?")
    assert llm.offered[1][0] == "none"
