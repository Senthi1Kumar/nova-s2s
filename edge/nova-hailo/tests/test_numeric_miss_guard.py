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
