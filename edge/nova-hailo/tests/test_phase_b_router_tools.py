"""Phase B acceptance matrix — pure offline coverage of plan bullets.

Maps each acceptance criterion from
docs/plans/2026-08-09-monday-demo-phase-b-router-tools.md
to a single focused test. No live APIs, no hailo_platform.

Detailed edge cases remain in test_edge_harness.py and test_oem_demo_offline.py.
"""
from __future__ import annotations

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.router import route
from nova_hailo.edge_harness.types import Intent


def test_acceptance_uk_pm_and_german_chancellor_web_search_rewrite():
    """current UK PM / German chancellor → web_search with rewritten query."""
    profile = CapabilityProfile.from_list(["web_search"])

    uk = route("who is the current Prime Minister of the UK", profile)
    assert uk is not None
    assert uk.intent is Intent.WEB_SEARCH
    assert uk.query == "current prime minister of the United Kingdom"

    de = route("who is the German chancellor", profile)
    assert de is not None
    assert de.intent is Intent.WEB_SEARCH
    assert de.query == "current chancellor of Germany"

    # Must not fall through to free-chat (None + LLM invents names).
    bare = route("current Prime Minister of the UK", profile)
    assert bare is not None and bare.intent is Intent.WEB_SEARCH


def test_acceptance_nvidia_tesla_multi_stock_dual_speak():
    """NVIDIA and Tesla multi stock → dual speak when evidence has both (unit)."""
    from nova_hailo.tools.search_mcp import _multi_stock_speak, _stock_search_query

    rewrite = _stock_search_query("stock price of NVIDIA and Tesla")
    assert rewrite is not None
    assert "NVIDIA NVDA" in rewrite
    assert "Tesla TSLA" in rewrite

    evidence = (
        "1. NVIDIA NVDA: NVIDIA stock price $875.50 trading at session highs. "
        "Tesla TSLA share price $248.30 after earnings."
    )
    speak = _multi_stock_speak("stock price of NVIDIA and Tesla", evidence)
    assert speak is not None
    assert "NVIDIA" in speak and "Tesla" in speak
    assert "$875.50" in speak and "$248.30" in speak
    assert "and" in speak


def test_acceptance_palantir_bitcoin_rewrite():
    """Palantir / Bitcoin → rewritten ticker/crypto search query path."""
    from nova_hailo.tools.search_mcp import _is_stock_query, _stock_search_query

    pltr = _stock_search_query("stock price of Palantir")
    assert pltr == "Palantir PLTR stock price USD"

    assert _is_stock_query("Bitcoin price")
    btc = _stock_search_query("Bitcoin price")
    assert btc is not None
    assert "Bitcoin" in btc and "BTC" in btc and "price" in btc.lower()
    assert "stock" not in btc.lower()


def test_acceptance_search_again_reruns_last_tool(monkeypatch):
    """search again after a search → re-executes last web_search query."""
    from nova_hailo.tools.oem_tools import OemToolGateway

    gw = OemToolGateway(enabled=["web_search"])
    calls: list[tuple[str, str]] = []

    def fake_execute(routed):
        calls.append((routed.intent.value, routed.query))
        return {
            "ok": True,
            "name": routed.intent.value,
            "status": "success",
            "speak": f"ok:{routed.query}",
            "result": {},
        }

    monkeypatch.setattr(gw._broker, "execute", fake_execute)

    out = gw.route_and_execute("search the web for hailo news")
    assert out is not None and out["ok"]
    first_q = gw._last_tool_query
    assert gw._last_tool_intent == "web_search"
    assert first_q

    out2 = gw.route_and_execute("search again")
    assert out2 is not None and out2["ok"]
    assert calls[-1] == ("web_search", first_q)


def test_acceptance_meta_web_search_tool_to_find_x():
    """use web search tool to find X → cleaned web_search query."""
    profile = CapabilityProfile.from_list(["web_search"])
    routed = route(
        "use the web search tool to find the current Prime Minister of the UK",
        profile,
    )
    assert routed is not None
    assert routed.intent is Intent.WEB_SEARCH
    assert routed.query == "current prime minister of the United Kingdom"

    simple = route("use the search tool to find hailo chips", profile)
    assert simple is not None
    assert simple.intent is Intent.WEB_SEARCH
    assert simple.query == "hailo chips"


def test_acceptance_drive_list_all_empty_query():
    """drive list-all → empty query (not full ASR as title search)."""
    from nova_hailo.edge_harness.tool_broker import drive_list_query

    for utt in (
        "files and folders in my drive",
        "my drive",
        "list files",
        "what's in my drive",
        "what are the files and folders in my drive",
        "what are the files in my drive",
    ):
        assert drive_list_query(utt) == "", utt

    # Specific needle still passes through.
    assert drive_list_query("find budget spreadsheet in my drive") == (
        "find budget spreadsheet in my drive"
    )


def test_acceptance_nvidia_only_stock_price():
    """Nvidia only / bare NVIDIA → stock price search."""
    profile = CapabilityProfile.from_list(["web_search"])

    for utt in ("Nvidia only", "only Nvidia", "NVIDIA only"):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.WEB_SEARCH, utt
        q = routed.query.lower()
        assert "stock price" in q and "nvidia" in q, (utt, routed.query)

    bare = route("NVIDIA", profile)
    assert bare is not None
    assert bare.intent is Intent.WEB_SEARCH
    assert "nvidia" in bare.query.lower()
    assert "stock price" in bare.query.lower()

def test_acceptance_multi_trailing_stock_routes():
    """NVIDIA and Tesla stock (trailing stock keyword) → WEB_SEARCH multi query."""
    profile = CapabilityProfile.from_list(["web_search"])
    for utt in (
        "NVIDIA and Tesla stock",
        "NVIDIA & Tesla stock",
        "Apple and Microsoft share",
    ):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.WEB_SEARCH, utt
        q = routed.query.lower()
        assert "stock price of" in q, (utt, routed.query)
        # Both entities present in multi query.
        assert " and " in q, (utt, routed.query)

    # No stock/share/price signal → do not invent multi stock route.
    bare = route("NVIDIA and Tesla", profile)
    assert bare is None


def test_acceptance_multi_speak_partial_no_invented_second():
    """Proximity miss for second ticker → honest partial; never invent $ from oil/etc."""
    from nova_hailo.tools.search_mcp import _multi_stock_speak

    # NVIDIA quote only; oil $72 is unrelated and "Tesla" never appears near it.
    evidence = (
        "1. NVIDIA NVDA closed at $875.50 on heavy volume after AI chip demand. "
        "Oil futures settled at $72.10. Broader tech mixed on the day with no auto quotes."
    )
    speak = _multi_stock_speak("NVIDIA and Tesla stock", evidence)
    assert speak is not None
    assert "NVIDIA" in speak and "$875.50" in speak
    assert "couldn't find a reliable second price" in speak.lower()
    assert "$72.10" not in speak
    assert "Tesla" not in speak or "second price" in speak.lower()
    # Must not dual-speak an invented second number.
    assert " and Tesla " not in speak


def test_acceptance_drive_what_are_files_folders_list_all():
    """Live gap: 'what are the files and folders in my drive' → empty Drive query."""
    from nova_hailo.edge_harness.tool_broker import drive_list_query

    for utt in (
        "what are the files and folders in my drive",
        "what are the files in my drive",
        "What are the files and folders in my Drive?",
        "Like uh, what are the files and folders in my drive?",
    ):
        assert drive_list_query(utt) == "", utt

    # Real search needles must not collapse to list-all.
    assert drive_list_query("files about Tesla") == "files about Tesla"

