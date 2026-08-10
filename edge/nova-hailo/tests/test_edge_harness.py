"""Canonical router, capability profiles, result compressor, and the
tool-broker-exclusivity invariant.

Callers: pytest. Exercises nova_hailo.edge_harness (types/policy/router/
result_compressor/tool_broker) directly, underneath the oem_tools facade
already covered by tests/test_oem_demo_offline.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.result_compressor import compress, speak_summary
from nova_hailo.edge_harness.router import CLARIFY_SPEAK, route
from nova_hailo.edge_harness.types import Intent, RoutedIntent, ToolResult

ROOT = Path(__file__).resolve().parents[1] / "nova_hailo"

# Any file outside tool_broker.py importing one of these is a broker bypass:
# a second code path that could invoke a tool without going through the
# allowlist/write-gate checks in ToolBroker.execute().
_TOOL_INVOKING_IMPORTS = re.compile(
    r"from nova_hailo\.tools\.search_mcp import|"
    r"from nova\.tools\.mcp\.(calendar|gmail|drive) import"
)


def test_tool_broker_is_the_only_tool_invoker():
    offenders = []
    for path in ROOT.rglob("*.py"):
        if path.name == "tool_broker.py":
            continue
        text = path.read_text()
        if _TOOL_INVOKING_IMPORTS.search(text):
            offenders.append(str(path.relative_to(ROOT.parent)))
    assert offenders == [], f"tool-invoking imports found outside tool_broker.py: {offenders}"


def test_route_returns_typed_routed_intent():
    profile = CapabilityProfile.from_list(["web_search"])
    routed = route("search the web for hailo", profile)
    assert isinstance(routed, RoutedIntent)
    assert routed.intent is Intent.WEB_SEARCH
    assert routed.intent == "web_search"  # str-enum: legacy string comparisons keep working


def test_route_no_match_returns_none():
    profile = CapabilityProfile.from_list([])
    assert route("that's a long drive isn't it", profile) is None


def test_route_clarify_bare_no_and_shorts():
    """Bare no/nope/n/what → canned clarify speak; no tool. Whole-utterance
    only so 'no problem' / 'not really' fall through (never-silent Phase A)."""
    profile = CapabilityProfile.from_list(["web_search"])
    for utt in ("no", "No.", "nope", "nah", "n", "N?", "what"):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.SMALLTALK, utt
        assert routed.slots == {"speak": CLARIFY_SPEAK}, utt
    for utt in ("no problem", "not really"):
        routed = route(utt, profile)
        is_clarify = (
            routed is not None
            and routed.intent is Intent.SMALLTALK
            and (routed.slots or {}).get("speak") == CLARIFY_SPEAK
        )
        assert not is_clarify, utt


def test_route_matches_status_and_situation_phrasings():
    """'current status of gas supply in Germany' must route to web_search,
    not fall through to the chat LLM (which can't search) -- these
    current-info shapes must match just as 'news' does."""
    profile = CapabilityProfile.from_list(["web_search"])
    assert route("what is the current status of gas supply in Germany", profile).intent == "web_search"
    assert route("what's the situation in Ukraine", profile).intent == "web_search"


def test_route_fills_calendar_day_slot():
    profile = CapabilityProfile.from_list(["check_calendar"])
    assert route("what's on my calendar tomorrow", profile).slots == {"day": "tomorrow"}
    assert route("what's on my calendar today", profile).slots == {"day": "today"}
    assert route("what's on my calendar", profile).slots == {"day": "week"}


def test_capability_profile_none_defaults_to_oem_readonly():
    profile = CapabilityProfile.from_list(None)
    assert profile.allows(Intent.WEB_SEARCH)
    assert profile.allows(Intent.CHECK_CALENDAR)


def test_capability_profile_empty_list_is_falsy_not_default():
    """enabled=[] must mean no tools, not 'fall back to the default profile'
    -- an empty list is falsy, and `enabled or DEFAULT` silently ignores it."""
    profile = CapabilityProfile.from_list([])
    assert not profile.allows(Intent.WEB_SEARCH)
    assert not profile.allows(Intent.CHECK_CALENDAR)


def test_capability_profile_write_gate():
    profile = CapabilityProfile.from_list(["send_email"], write_enabled=False)
    assert not profile.allows_write(Intent.SEND_EMAIL)
    assert profile.allows_write(Intent.WEB_SEARCH)  # non-write intents always pass the write gate


def test_tool_result_to_dict_matches_legacy_shape():
    ok = ToolResult(ok=True, name="identity", status="success", speak="hi", result=None)
    assert ok.to_dict() == {"ok": True, "name": "identity", "status": "success", "speak": "hi", "result": None}
    bad = ToolResult(ok=False, name="web_search", status="unavailable", speak="no", reason="missing_key")
    assert bad.to_dict()["reason"] == "missing_key"


def test_speak_summary_prefers_explicit_speak_field():
    assert speak_summary({"speak": "You have three meetings."}) == "You have three meetings."


def test_speak_summary_falls_back_to_bulleted_items():
    payload = {"events": [{"title": "Team sync"}, {"title": "Dentist"}]}
    assert speak_summary(payload) == "Team sync; Dentist"


def test_speak_summary_empty_payload_says_done():
    assert speak_summary({}) == "Done."


def test_compress_is_bounded_and_never_truncates_the_instruction_line():
    payload = {"events": [{"title": "x" * 200}]}
    block = compress("check_calendar", payload, max_chars=150)
    assert len(block) <= 150
    assert block.endswith(
        "instruction=Speak only from the items above; do not invent details."
    )
    assert block.startswith("[[tool_result name=check_calendar]]")


def test_compress_no_items_says_so():
    block = compress("check_calendar", {})
    assert "(no items)" in block


def test_route_current_leader_forces_web_search():
    """UK PM / German chancellor must never free-chat (invents names)."""
    profile = CapabilityProfile.from_list(["web_search"])
    cases = [
        ("who is the current Prime Minister of the UK", "current prime minister of the United Kingdom"),
        ("current Prime Minister of the UK", "current prime minister of the United Kingdom"),
        ("who is the German chancellor", "current chancellor of Germany"),
        ("who is the current chancellor of Germany", "current chancellor of Germany"),
        ("who's the president of the US", "current president of the United States"),
        ("current PM of Britain", "current prime minister of the United Kingdom"),
    ]
    for utt, expected_q in cases:
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.WEB_SEARCH, utt
        assert routed.query == expected_q, (utt, routed.query)


def test_route_meta_search_strips_to_object():
    """'use the web search tool to find X' → web_search with cleaned object."""
    profile = CapabilityProfile.from_list(["web_search"])
    routed = route(
        "use the web search tool to find the current Prime Minister of the UK",
        profile,
    )
    assert routed is not None
    assert routed.intent is Intent.WEB_SEARCH
    assert routed.query == "current prime minister of the United Kingdom"

    routed2 = route("use the search tool to find hailo chips", profile)
    assert routed2 is not None
    assert routed2.intent is Intent.WEB_SEARCH
    assert routed2.query == "hailo chips"

    # No clear object → leave for Task 2 (stateful); do not force web_search here.
    assert route("use the web search tool", profile) is None
    assert route("search again", profile) is None


def test_route_stock_nvidia_only_and_multi_entities():
    """'Nvidia only' / multi entity alts expand to stock price queries."""
    profile = CapabilityProfile.from_list(["web_search"])

    for utt in ("Nvidia only", "only Nvidia", "NVIDIA only"):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.WEB_SEARCH, utt
        assert routed.query.lower() == "stock price of nvidia" or routed.query.lower().startswith(
            "stock price of nvidia"
        ), (utt, routed.query)
        assert "nvidia" in routed.query.lower()

    multi = route("stock price of NVIDIA and Tesla", profile)
    assert multi is not None
    assert multi.intent is Intent.WEB_SEARCH
    assert "nvidia" in multi.query.lower()
    assert "tesla" in multi.query.lower()

    # Trailing stock keyword (live gap): entities first, then "stock".
    trailing = route("NVIDIA and Tesla stock", profile)
    assert trailing is not None
    assert trailing.intent is Intent.WEB_SEARCH
    assert "nvidia" in trailing.query.lower()
    assert "tesla" in trailing.query.lower()
    assert "stock price of" in trailing.query.lower()

    multi2 = route("stock price of palantir and bitcoin", profile)
    assert multi2 is not None
    assert multi2.intent is Intent.WEB_SEARCH
    assert "palantir" in multi2.query.lower()
    assert "bitcoin" in multi2.query.lower()

    bare = route("PLTR", profile)
    assert bare is not None
    assert bare.intent is Intent.WEB_SEARCH
    assert "pltr" in bare.query.lower()

    bare_btc = route("Bitcoin", profile)
    assert bare_btc is not None
    assert bare_btc.intent is Intent.WEB_SEARCH
    assert "bitcoin" in bare_btc.query.lower()


def test_is_search_again_phrases():
    """Pure helper matches search-again / pure meta; rejects object meta."""
    from nova_hailo.edge_harness.router import is_search_again

    positives = [
        "search again",
        "search again please",
        "try again",
        "look it up again",
        "use the search tool again",
        "use the web search tool",
        "use the web search tool again",
        "please search again",
        "can you try searching again?",
        "try searching",
    ]
    for utt in positives:
        assert is_search_again(utt), utt

    negatives = [
        "",
        "search the web for hailo",
        "use the web search tool to find the UK PM",
        "what is the stock price of nvidia",
        "hello",
        "try searching for bitcoin",
    ]
    for utt in negatives:
        assert not is_search_again(utt), utt


def test_gateway_last_tool_memory_and_search_again(monkeypatch):
    """route_and_execute stores real tools; search-again re-runs last query."""
    from nova_hailo.tools.oem_tools import OemToolGateway

    gw = OemToolGateway(enabled=["web_search", "deep_research", "check_calendar"])
    calls: list[tuple[str, str]] = []

    def fake_execute(routed):
        calls.append((routed.intent.value, routed.query))
        return {
            "ok": True,
            "name": routed.intent.value,
            "status": "success",
            "speak": f"ok:{routed.query}",
            "result": {"q": routed.query},
        }

    monkeypatch.setattr(gw._broker, "execute", fake_execute)

    # Real tool success → memory
    out = gw.route_and_execute("search the web for current prime minister of the UK")
    assert out is not None and out["ok"]
    assert gw._last_tool_intent == "web_search"
    assert "prime minister" in (gw._last_tool_query or "").lower() or "uk" in (
        gw._last_tool_query or ""
    ).lower() or "search the web" in (gw._last_tool_query or "").lower()
    first_q = gw._last_tool_query
    assert first_q
    assert calls[-1][0] == "web_search"

    # Search-again re-executes last intent + last query (no new route object)
    n_before = len(calls)
    out2 = gw.route_and_execute("search again")
    assert out2 is not None and out2["ok"]
    assert len(calls) == n_before + 1
    assert calls[-1] == ("web_search", first_q)
    assert out2["speak"] == f"ok:{first_q}"

    # Pure meta re-run
    out3 = gw.route_and_execute("use the web search tool")
    assert out3 is not None and out3["ok"]
    assert calls[-1] == ("web_search", first_q)

    # Identity must not overwrite memory
    mem_intent, mem_q = gw._last_tool_intent, gw._last_tool_query
    id_out = gw.route_and_execute("what is your name")
    assert id_out is not None and id_out["ok"]
    assert gw._last_tool_intent == mem_intent
    assert gw._last_tool_query == mem_q

    # Failed real tool does not overwrite memory
    def fail_execute(routed):
        calls.append((routed.intent.value, routed.query))
        return {
            "ok": False,
            "name": routed.intent.value,
            "status": "unavailable",
            "speak": "no",
            "reason": "missing_key",
        }

    monkeypatch.setattr(gw._broker, "execute", fail_execute)
    gw.route_and_execute("search the web for bitcoin price")
    assert gw._last_tool_intent == mem_intent
    assert gw._last_tool_query == mem_q


def test_gateway_search_again_without_prior_returns_none(monkeypatch):
    """No last search → search again falls through; route returns None."""
    from nova_hailo.tools.oem_tools import OemToolGateway

    gw = OemToolGateway(enabled=["web_search"])
    called = []

    def boom(routed):
        called.append(routed)
        raise AssertionError("broker should not run for bare search-again with no memory")

    monkeypatch.setattr(gw._broker, "execute", boom)
    assert gw._last_tool_intent is None
    assert gw.route_and_execute("search again") is None
    assert gw.route_and_execute("use the web search tool") is None
    assert called == []


def test_drive_list_query_list_all_patterns():
    """List-all ASR phrases must become empty Drive query (not title search)."""
    from nova_hailo.edge_harness.tool_broker import drive_list_query

    list_all = [
        "my drive",
        "My Drive",
        "files and folders in my drive",
        "files and folders",
        "list files",
        "what's in my drive",
        "whats in my drive",
        "what are the files and folders in my drive",
        "what are the files in my drive",
        "files in drive",
        "files in my drive",
        "show my drive",
        "show my recent Drive files",
        "list files in my drive",
        "please show me the files in my drive",
        "can you list the folders in drive?",
        "documents in my drive",
        "folders in my drive",
        "drive",
        "Drive?",
        "google drive",
        # Leading ASR fillers stripped before list-all match.
        "Like uh, what are the files and folders in my drive?",
        "uh, what are the files in my drive",
        "um so well okay list files",
        "hey, what's in my drive",
        "ok, my drive",
    ]
    for utt in list_all:
        assert drive_list_query(utt) == "", utt

    # Specific search asks keep a non-empty needle for title search.
    specifics = [
        "find budget spreadsheet in my drive",
        "drive files named report",
        "files called presentation",
        "open the Q3 report in drive",
        "search for invoice in drive",
        "files about Tesla",
        "like files about Tesla",
    ]
    for utt in specifics:
        out = drive_list_query(utt)
        assert out == utt, (utt, out)

    assert drive_list_query("") == ""
    assert drive_list_query("   ") == ""


def test_route_list_drive_list_all_and_broker_empty_query(monkeypatch):
    """Router LIST_DRIVE_FILES + broker executes Drive with query='' for list-all."""
    import sys
    from types import ModuleType

    from nova_hailo.edge_harness.tool_broker import ToolBroker

    profile = CapabilityProfile.from_list(["list_drive_files"])
    for utt in (
        "files and folders in my drive",
        "my drive",
        "list files",
        "what's in my drive",
    ):
        routed = route(utt, profile)
        assert routed is not None, utt
        assert routed.intent is Intent.LIST_DRIVE_FILES, utt
        # Router keeps original ASR on RoutedIntent; broker normalizes at execute.
        assert routed.query.strip() == utt.strip(), (utt, routed.query)

    broker = ToolBroker(profile)
    seen: list[str] = []

    class FakeDrive:
        def execute(self, query: str = "") -> dict:
            seen.append(query)
            return {
                "status": "success",
                "file_count": 0,
                "files": [],
                "speak": "Recent Drive files: none.",
                "source": "fake",
            }

    monkeypatch.setattr(broker, "_ensure_workspace_auth", lambda: None)

    # Stub only the drive MCP module so _mcp_drive's late import needs no dotenv.
    fake_drive = ModuleType("nova.tools.mcp.drive")
    fake_drive.ListDriveFilesTool = FakeDrive  # type: ignore[attr-defined]
    for name in ("nova", "nova.tools", "nova.tools.mcp"):
        if name not in sys.modules:
            pkg = ModuleType(name)
            pkg.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = pkg
    monkeypatch.setitem(sys.modules, "nova.tools.mcp.drive", fake_drive)

    for utt in ("files and folders in my drive", "my drive"):
        seen.clear()
        routed = route(utt, profile)
        assert routed is not None
        out = broker.execute(routed)
        assert out["ok"] is True, out
        assert seen == [""], (utt, seen)

    # Specific search still forwards non-empty query.
    seen.clear()
    specific = "find budget spreadsheet in my drive"
    routed = route(specific, profile)
    assert routed is not None and routed.intent is Intent.LIST_DRIVE_FILES
    out = broker.execute(routed)
    assert out["ok"] is True
    assert seen == [specific], seen
