"""Tests for the external/stub NovaTools: websearch, weather, research, stubs.

Fallback orchestration (Brave -> Serper) is tested by injecting a failing
primary provider (monkeypatching the instance method), not by mocking a
network library — this is orchestration logic, not business logic, per the
task brief. Missing-key paths are tested by constructing tools with explicit
``None`` keys, asserting a graceful non-raising result. Real network calls
against Open-Meteo (no key needed) are exercised directly; real calls to
keyed providers are marked ``slow`` and skipped when the key is absent.
"""
from __future__ import annotations

import os

import pytest

from nova.tools.research import ResearchTool
from nova.tools.stubs import CalendarStubTool, GmailStubTool, SpotifyStubTool
from nova.tools.weather import WeatherTool
from nova.tools.websearch import WebSearchTool

ALL_TOOL_CLASSES = [
    WebSearchTool,
    WeatherTool,
    ResearchTool,
    GmailStubTool,
    CalendarStubTool,
    SpotifyStubTool,
]


# ---------- schema shape (same pattern as Task 3's test_vehicle.py) ----------


@pytest.mark.parametrize("tool_cls", ALL_TOOL_CLASSES)
def test_function_tool_schema_well_formed(tool_cls):
    tool = tool_cls()
    schema = tool.to_function_tool()

    assert schema["type"] == "function"
    assert schema["name"] == tool.name
    assert isinstance(schema["name"], str) and schema["name"]
    assert isinstance(schema["description"], str) and schema["description"]
    assert isinstance(schema["parameters"], dict)
    assert schema["parameters"]["type"] == "object"
    assert "properties" in schema["parameters"]


# ---------- websearch: Brave -> Serper fallback orchestration ----------


def test_websearch_uses_brave_when_it_succeeds():
    tool = WebSearchTool(brave_api_key="fake-brave-key", serper_api_key="fake-serper-key")
    tool._brave_search = lambda query, n: [{"title": "t", "url": "u", "snippet": "s"}]
    tool._serper_search = lambda query, n: (_ for _ in ()).throw(AssertionError("serper should not be called"))

    result = tool.execute(query="test query")

    assert result["status"] == "success"
    assert result["provider"] == "brave"
    assert result["results"][0]["title"] == "t"
    assert "speak" in result


def test_websearch_falls_back_to_serper_when_brave_fails():
    tool = WebSearchTool(brave_api_key="fake-brave-key", serper_api_key="fake-serper-key")
    tool._brave_search = lambda query, n: (_ for _ in ()).throw(RuntimeError("brave 401"))
    tool._serper_search = lambda query, n: [{"title": "t2", "url": "u2", "snippet": "s2"}]

    result = tool.execute(query="test query")

    assert result["status"] == "success"
    assert result["provider"] == "serper"
    assert result["results"][0]["title"] == "t2"


def test_websearch_news_spectrum_one_per_category():
    tool = WebSearchTool(brave_api_key="fake-brave-key", serper_api_key="fake-serper-key")

    def fake_news(query, n):
        if "politics" in query:
            return [{"title": "Council passes metro bill", "url": "https://a.com/1", "snippet": "vote"}]
        if "traffic" in query:
            return [{"title": "ORR jam after crash", "url": "https://a.com/2", "snippet": "delay"}]
        if "business" in query:
            return [{"title": "Startup raises funding", "url": "https://a.com/3", "snippet": "$"}]
        if "sports" in query:
            return [{"title": "RCB wins home match", "url": "https://a.com/4", "snippet": "cricket"}]
        if "crime" in query:
            return [{"title": "Police arrest fraud ring", "url": "https://a.com/5", "snippet": "case"}]
        return [{"title": "Bengaluru News: Latest Bangalore News on Politics", "url": "https://agg.com", "snippet": "portal"}]

    tool._brave_news = fake_news
    tool._serper_news = lambda q, n: (_ for _ in ()).throw(AssertionError("serper news unused"))
    tool._brave_search = lambda q, n: (_ for _ in ()).throw(AssertionError("web unused"))
    tool._serper_search = lambda q, n: (_ for _ in ()).throw(AssertionError("web unused"))

    result = tool.execute(query="news in Bangalore", place="Bengaluru", category="general")
    assert result["status"] == "success"
    assert result["mode"] == "news_spectrum"
    cats = {r.get("category") for r in result["results"]}
    assert {"politics", "traffic", "business", "sports", "crime"} <= cats
    assert "speak" in result and "Today in Bengaluru" in result["speak"]
    assert "https://" not in result["speak"]
    assert all(len(r.get("url", "")) < 60 for r in result["results"])


def test_websearch_category_politics():
    tool = WebSearchTool(brave_api_key="k", serper_api_key="k")
    tool._brave_news = lambda q, n: [
        {"title": "Mayor announces budget", "url": "https://hindu.com/x", "snippet": "civic"}
    ]
    tool._serper_news = lambda q, n: []
    tool._brave_search = lambda q, n: []
    tool._serper_search = lambda q, n: []
    out = tool.execute(query="politics news", place="Bengaluru", category="politics")
    assert out["mode"] == "news"
    assert out["results"][0]["category"] == "politics"
    assert "politics" in out["speak"].lower()


def test_websearch_unavailable_when_both_providers_fail():
    tool = WebSearchTool(brave_api_key="fake-brave-key", serper_api_key="fake-serper-key")
    tool._brave_search = lambda query, n: (_ for _ in ()).throw(RuntimeError("brave down"))
    tool._serper_search = lambda query, n: (_ for _ in ()).throw(RuntimeError("serper down"))

    result = tool.execute(query="test query")

    assert result["status"] == "unavailable"
    assert "reason" in result


def test_websearch_missing_keys_degrades_gracefully_without_raising():
    tool = WebSearchTool(brave_api_key=None, serper_api_key=None)

    result = tool.execute(query="anything")

    assert result["status"] == "unavailable"
    assert "reason" in result


# ---------- research (Tavily): missing key ----------


def test_research_missing_key_degrades_gracefully_without_raising():
    tool = ResearchTool(tavily_api_key=None)

    result = tool.execute(query="anything")

    assert result["status"] == "unavailable"
    assert "TAVILY_API_KEY" in result["reason"]


# ---------- weather (Open-Meteo, no key): place-not-found ----------


@pytest.mark.live
def test_weather_place_not_found_degrades_gracefully_without_raising():
    tool = WeatherTool()

    result = tool.execute(place="Zzzznotarealplacexyz123")

    assert result["status"] == "not_found"


@pytest.mark.live
def test_weather_real_place_returns_current_conditions():
    tool = WeatherTool()

    result = tool.execute(place="Bengaluru")

    assert result["status"] == "success"
    assert isinstance(result["temp_c"], (int, float))
    assert isinstance(result["condition"], str) and result["condition"]


@pytest.mark.live
def test_weather_bangalore_alias_resolves():
    tool = WeatherTool()

    result = tool.execute(place="Bangalore")

    assert result["status"] == "success"
    assert "Bengaluru" in result["place"] or result["place"]
    assert isinstance(result["temp_c"], (int, float))


# ---------- stubs: canned shape ----------


def test_gmail_stub_returns_canned_unread_shape():
    result = GmailStubTool().execute()
    assert result["status"] == "success"
    assert isinstance(result["unread_count"], int) and result["unread_count"] > 0
    assert all({"from", "subject", "preview"} <= r.keys() for r in result["messages"])


def test_calendar_stub_returns_canned_events_shape():
    result = CalendarStubTool().execute()
    assert result["status"] == "success"
    assert all({"title", "start"} <= e.keys() for e in result["events"])


def test_spotify_stub_returns_canned_now_playing_shape():
    result = SpotifyStubTool().execute(query="lofi beats")
    assert result["status"] == "success"
    assert result["now_playing"] == "lofi beats"


def test_spotify_stub_defaults_now_playing_when_no_query():
    result = SpotifyStubTool().execute()
    assert result["status"] == "success"
    assert result["now_playing"]


# ---------- real API sanity calls (marked slow; skip cleanly without keys) ----------


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("BRAVE_API_KEY"), reason="BRAVE_API_KEY not set in shell env")
def test_websearch_real_brave_call():
    tool = WebSearchTool()
    result = tool.execute(query="LiteRT-LM")
    assert result["status"] == "success"
    assert result["results"]


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("TAVILY_API_KEY"), reason="TAVILY_API_KEY not set in shell env")
def test_research_real_tavily_call():
    tool = ResearchTool()
    result = tool.execute(query="What is LiteRT-LM?")
    assert result["status"] == "success"
    assert result["sources"]
