"""Octopus-style compact codec + token map. No Hailo."""
from __future__ import annotations

from nova_hailo.edge_harness.compact_codec import (
    encode_call,
    encode_routed,
    parse_call,
    token_for_tool,
    tool_for_token,
)
from nova_hailo.edge_harness.controller import host_turn
from nova_hailo.tools.oem_tools import OemToolGateway


def test_token_map_roundtrip_names():
    assert tool_for_token("t0") == "web_search"
    assert token_for_tool("web_search") == "t0"
    assert token_for_tool("respond") == "t5"
    assert tool_for_token("t9") is None


def test_parse_named_args_and_reject_junk():
    ok = parse_call('t0(query="best coffee shops Los Gatos")')
    assert ok == {
        "token": "t0",
        "tool": "web_search",
        "args": {"query": "best coffee shops Los Gatos"},
    }
    cal = parse_call('t2(day="today")')
    assert cal and cal["tool"] == "check_calendar"
    assert parse_call('t0()') is None  # missing required query
    assert parse_call('t0(query="x", hack="1")') is None
    assert parse_call('{"type":"call","tool":"web_search"}') is None
    assert parse_call("") is None


def test_encode_omits_empty_optionals():
    assert encode_call("t2") == "t2()"
    assert encode_routed("web_search", "news in Delhi") == 't0(query="news in Delhi")'
    assert encode_routed("identity", "who are you").startswith("t5(message=")


def test_host_turn_emits_codec_for_search(monkeypatch):
    gw = OemToolGateway(enabled=["web_search"])

    def fake_execute(routed):
        return {
            "ok": True,
            "name": routed.intent.value,
            "status": "success",
            "speak": "ok",
            "result": {},
        }

    monkeypatch.setattr(gw._broker, "execute", fake_execute)
    out = host_turn(gw, "Latest news in uh Delhi")
    assert out["codec"] == 't0(query="Latest news in uh Delhi")'
    coffee = host_turn(gw, "Best coffee shops in Los Gatos")
    assert coffee["codec"] and coffee["codec"].startswith("t0(")
    assert parse_call(coffee["codec"])["tool"] == "web_search"
