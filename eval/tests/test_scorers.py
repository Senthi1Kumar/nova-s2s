"""Unit tests for eval.scorers — no models, no LiteRT."""
from __future__ import annotations

from eval.scorers import (
    aggregate_no_tool_pr,
    has_forbidden_tool_markup,
    is_malformed_call,
    latency_percentiles,
    normalize_auth_status,
    score_argument_slots,
    score_auth_status,
    score_duplicate_execution,
    score_factual_speak,
    score_no_tool_pair,
    score_tool_name,
    score_turn,
)


def test_tool_name_exact_and_both_none():
    assert score_tool_name("check_email", "check_email")
    assert score_tool_name(None, None)
    assert not score_tool_name("check_email", "check_calendar")
    assert not score_tool_name("check_email", None)


def test_argument_slots_matched_missing_wrong():
    ok = score_argument_slots(
        {"zone": "driver", "on": True, "target_temp_c": 22},
        {"zone": "driver", "on": True, "target_temp_c": 22},
    )
    assert ok["ok"]
    assert set(ok["matched"]) == {"zone", "on", "target_temp_c"}

    miss = score_argument_slots({"zone": "driver"}, {"zone": "driver", "on": True})
    assert not miss["ok"]
    assert miss["missing"] == ["on"]

    wrong = score_argument_slots({"zone": "passenger"}, {"zone": "driver"})
    assert not wrong["ok"]
    assert wrong["wrong"] == ["zone"]


def test_no_tool_precision_recall():
    counts = [
        score_no_tool_pair(True, True),
        score_no_tool_pair(True, False),
        score_no_tool_pair(False, True),
        score_no_tool_pair(False, False),
    ]
    agg = aggregate_no_tool_pr(counts)
    assert agg["tp"] == 1 and agg["fp"] == 1 and agg["fn"] == 1
    assert agg["precision"] == 0.5
    assert agg["recall"] == 0.5


def test_malformed_and_unknown_tool():
    known = {"check_email", "send_payment"}
    assert is_malformed_call(None, known)
    assert is_malformed_call({"name": ""}, known)
    assert is_malformed_call({"name": "hack_bank", "args": {}}, known)
    assert is_malformed_call({"name": "check_email", "args": "oops"}, known)
    assert not is_malformed_call({"name": "check_email", "args": {"mode": "unread"}}, known)


def test_factual_speak_requires_literal_number():
    payload = {"fuel_pct": 68, "range_km": 210}
    good = score_factual_speak("Fuel is at 68 percent, about 210 kilometers.", payload)
    assert good["ok"]
    bad = score_factual_speak("Fuel is at eight percent.", payload, keys=["fuel_pct"])
    assert not bad["ok"]
    assert "fuel_pct" in bad["missing"]


def test_forbidden_tool_markup():
    assert has_forbidden_tool_markup("Hello <|tool_call_start|>x<|tool_call_end|>")
    assert has_forbidden_tool_markup("<|tool_calls_section_begin|>")
    assert not has_forbidden_tool_markup("Paid 50 INR to Chai Point.")


def test_duplicate_execution():
    calls = [
        {"name": "set_hvac", "args": {"zone": "driver", "on": True}},
        {"name": "set_hvac", "args": {"zone": "driver", "on": True}},
    ]
    d = score_duplicate_execution(calls)
    assert not d["ok"]
    assert "set_hvac" in d["duplicates"]
    assert score_duplicate_execution(calls[:1])["ok"]


def test_auth_status_aliases():
    assert normalize_auth_status("step_up") == "step_up_required"
    assert normalize_auth_status("reject") == "denied"
    assert score_auth_status("step_up", "step_up_required")
    assert score_auth_status("reject", "denied")
    assert not score_auth_status("accept", "reject")


def test_latency_percentiles_sorted():
    out = latency_percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert out["n"] == 10
    assert out["p50"] == 50
    assert out["p90"] == 90
    assert latency_percentiles([])["p50"] == 0.0


def test_score_turn_bundle_no_tool():
    scored = score_turn(
        expect={"tool": None, "no_tool": True},
        predicted_tool=None,
        predicted_no_tool=True,
        speak="Sure.",
    )
    assert scored["tool_ok"]
    assert scored["no_tool"]["tp"] == 1
    assert not scored["forbidden_markup"]
