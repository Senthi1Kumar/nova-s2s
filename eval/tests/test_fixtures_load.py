"""Load + schema checks for the s2s-native fixture corpus.

Callers: scripts/run_tests.sh run_eval → pytest eval/tests/.
Reads eval/fixtures/s2s_turns.jsonl only (synthetic transcripts).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import CORPUS_VERSION
from eval.run_corpus import KNOWN_TOOLS, load_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "s2s_turns.jsonl"

REQUIRED_TOP = {"id", "transcript", "expect", "tags"}
AUTH_OK = {"accept", "step_up", "step_up_required", "reject", "denied", "bypass"}


def test_corpus_version_is_int():
    assert isinstance(CORPUS_VERSION, int)
    assert CORPUS_VERSION >= 1


def test_fixtures_file_exists_and_nonempty():
    assert FIXTURES.is_file()
    rows = load_fixtures(FIXTURES)
    assert len(rows) >= 40


def test_each_fixture_has_required_fields_and_unique_ids():
    rows = load_fixtures(FIXTURES)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    for row in rows:
        assert REQUIRED_TOP <= set(row.keys())
        assert isinstance(row["transcript"], str) and row["transcript"].strip()
        assert isinstance(row["expect"], dict)
        assert isinstance(row["tags"], list) and row["tags"]


def test_gold_tools_match_live_registry_names():
    rows = load_fixtures(FIXTURES)
    for row in rows:
        tool = row["expect"].get("tool")
        if tool is None:
            continue
        assert tool in KNOWN_TOOLS, f"{row['id']}: unknown tool {tool!r}"


def test_coverage_tags_present():
    rows = load_fixtures(FIXTURES)
    all_tags = {t for r in rows for t in r["tags"]}
    for needed in (
        "clean",
        "stt",
        "ood",
        "followup",
        "confirm",
        "payment",
        "auth",
        "no_tool",
        "unsupported",
        "correction",
    ):
        assert needed in all_tags, f"missing tag {needed}"


def test_auth_and_no_tool_expect_shapes():
    rows = load_fixtures(FIXTURES)
    saw_auth = saw_no_tool = saw_unsupported = False
    for row in rows:
        exp = row["expect"]
        if "auth_status" in exp:
            saw_auth = True
            assert exp["auth_status"] in AUTH_OK
            assert exp.get("tool") == "send_payment"
        if exp.get("no_tool"):
            saw_no_tool = True
        if exp.get("unsupported"):
            saw_unsupported = True
    assert saw_auth and saw_no_tool and saw_unsupported


def test_jsonl_roundtrip_raw_lines():
    raw = FIXTURES.read_text().strip().splitlines()
    assert raw
    for i, line in enumerate(raw, start=1):
        obj = json.loads(line)
        assert "id" in obj, f"line {i}"


def test_args_keys_look_like_schema_slots():
    by_id = {r["id"]: r for r in load_fixtures(FIXTURES)}
    assert by_id["hvac_clean_14"]["expect"]["args"]["zone"] == "driver"
    assert by_id["weather_clean_10"]["expect"]["args"]["place"] == "Bangalore"
    assert by_id["pay_accept_39"]["expect"]["args"]["amount"] == 50
    assert by_id["drive_create_25"]["expect"]["args"]["name"] == "Nova Demo"


@pytest.mark.parametrize(
    "tool",
    sorted(
        {
            "check_email",
            "check_calendar",
            "get_weather",
            "web_search",
            "set_hvac",
            "send_payment",
            "list_drive_files",
            "create_drive_folder",
        }
    ),
)
def test_at_least_one_fixture_per_core_tool(tool: str):
    rows = load_fixtures(FIXTURES)
    assert any(r["expect"].get("tool") == tool for r in rows), tool
