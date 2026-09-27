"""Jev router with a fake client. No network. Regex tests stay on the default gateway."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from nova_hailo.edge_harness.jev_router import compose_jev_answers, route_with_jev
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.types import Intent
from nova_hailo.tools.oem_tools import OemToolGateway


def _response(nouls: dict[str, float], choice: str | None = None):
    answers = {
        key: SimpleNamespace(noul=value, choice=None) for key, value in nouls.items()
    }
    if choice is not None:
        answers["calendar_day"] = SimpleNamespace(noul=None, choice=choice)
    return SimpleNamespace(answers=answers)


class _Client:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def system_one(self, state, questions, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _profile(*enabled: str) -> CapabilityProfile:
    return CapabilityProfile.from_list(list(enabled))


def test_compose_calendar_tomorrow():
    profile = _profile("check_calendar", "web_search")
    routed = compose_jev_answers(
        _response({"check_calendar": 0.91, "web_search": 0.1}, choice="tomorrow"),
        "what's on my calendar tomorrow",
        profile,
    )
    assert routed is not None and len(routed) == 1
    assert routed[0].intent is Intent.CHECK_CALENDAR
    assert routed[0].slots == {"day": "tomorrow"}


def test_compound_executes_both_tools():
    client = _Client(
        _response({"check_calendar": 0.9, "web_search": 0.82}, choice="tomorrow")
    )
    gw = OemToolGateway(
        enabled=["check_calendar", "web_search"],
        router="jev",
        jev_client=client,
    )
    calls = []

    def fake_execute(routed):
        calls.append(routed)
        return {
            "ok": True,
            "name": routed.intent.value,
            "status": "success",
            "speak": routed.intent.value,
            "result": {},
        }

    gw._broker.execute = fake_execute
    out = gw.route_and_execute("calendar tomorrow and the news")
    assert client.calls == 1
    assert [c.intent for c in calls] == [Intent.CHECK_CALENDAR, Intent.WEB_SEARCH]
    assert calls[0].slots["day"] == "tomorrow"
    assert out["name"] == "compound"
    assert out["speak"] == "check_calendar web_search"
    assert [a["name"] for a in out["result"]["actions"]] == [
        "check_calendar",
        "web_search",
    ]


def test_low_confidence_falls_back_to_regex():
    client = _Client(_response({"check_calendar": 0.4, "web_search": 0.2}))
    gw = OemToolGateway(
        enabled=["check_calendar", "web_search"],
        router="jev",
        jev_client=client,
    )
    assert route_with_jev("what's on my calendar", gw.profile, client=client) is None
    assert gw.route("what's on my calendar tomorrow") == "check_calendar"


def test_client_error_falls_back_to_regex():
    client = _Client(error=RuntimeError("down"))
    gw = OemToolGateway(enabled=["web_search"], router="jev", jev_client=client)
    assert gw.route("Search the web for hailo") == "web_search"
    assert client.calls == 1


def test_disabled_calendar_does_not_call_mcp():
    client = _Client(_response({"asks_disabled_tool": 0.93, "web_search": 0.05}))
    gw = OemToolGateway(enabled=["web_search"], router="jev", jev_client=client)
    with patch.object(gw._broker, "_mcp_calendar") as mcp:
        out = gw.route_and_execute("What's on my calendar?")
    mcp.assert_not_called()
    assert out["name"] == "capability_unavailable"
    assert "calendar" in out["speak"].lower()


def test_missing_key_skips_jev(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    gw = OemToolGateway(enabled=["web_search"], router="jev")
    assert gw.route("Search the web for hailo") == "web_search"


def test_live_jev_client_is_created_once_and_reused(monkeypatch):
    # A client per call paid TCP+TLS setup on every routed turn (~0.2 s from
    # the Pi); one keep-alive client pays it once per process.
    import sys
    import types

    created = []

    class _FakeClient:
        def __init__(self, **kw):
            created.append(kw)

        def system_one(self, state, questions, **kw):
            return {"answers": {}}

    fake = types.ModuleType("typesafe_sdk")
    fake.TypeSafeClient = _FakeClient
    fake.RetryPolicy = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "typesafe_sdk", fake)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    gw = OemToolGateway(enabled=["web_search"], router="jev", jev_warm=False)
    gw.route("tell me the latest ai news")
    gw.route("how are you")
    assert len(created) == 1
