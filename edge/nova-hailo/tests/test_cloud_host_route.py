"""The cloud turn's host shortcut (the clock) must not cost a network round trip.

On the Pi the Jev/TypeSafe router costs ~0.7 s per call, paid at the
start of every cloud turn only to learn whether the question was about the
clock -- which the regex already answers, and the model sees the time anyway.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

_hp = types.ModuleType("hailo_platform")
_hp.VDevice = MagicMock()
_genai = types.ModuleType("hailo_platform.genai")
_genai.LLM = MagicMock()
_genai.Speech2Text = MagicMock()
_genai.Speech2TextTask = MagicMock()
_hp.genai = _genai
sys.modules.setdefault("hailo_platform", _hp)
sys.modules.setdefault("hailo_platform.genai", _genai)

from nova_hailo.pipeline import NovaPipeline  # noqa: E402


class _NoNetworkGateway:
    def route(self, query):
        raise AssertionError("cloud turn must not call the network router")


def _pipeline():
    p = NovaPipeline.__new__(NovaPipeline)
    p.oem_gateway = _NoNetworkGateway()
    return p


def test_clock_question_is_host_routed_without_the_network_router():
    assert _pipeline()._cloud_host_route("What time is it right now?") == "get_datetime"


def test_other_questions_go_to_the_cloud_model_without_the_network_router():
    p = _pipeline()
    for q in ("How are you?", "Tell me the latest AI news", "What's the weather in Delhi?"):
        assert p._cloud_host_route(q) is None


def test_clock_regex_accepts_trailing_now_but_not_other_places():
    p = _pipeline()
    assert p._cloud_host_route("What time is it now?") == "get_datetime"
    assert p._cloud_host_route("what's the time right now") == "get_datetime"
    # The host clock only knows local time; elsewhere is the model's job.
    assert p._cloud_host_route("What time is it in Tokyo?") is None
