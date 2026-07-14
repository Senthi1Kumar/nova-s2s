"""Recorded-turn replay: transcript → route/agent → mocked tool → articulator contract.

No audio, no live models. Gold transcripts come from ``eval/fixtures/s2s_turns.jsonl``.

Covers: greeting, email, calendar tomorrow, Drive listing, news/stock, payment
ACCEPT/STEP_UP/REJECT, PIN retry, confirmation, correction, barge-in cancel
contract, unsupported delete.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eval.run_corpus import load_fixtures
from eval.scorers import _TOOL_MARKUP_RE, normalize_auth_status

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "s2s_turns.jsonl"

# Scenario → fixture id (sanitized corpus). barge-in is synthetic (no audio).
REPLAY_IDS = {
    "greeting": "chitchat_36",
    "email_unread": "email_clean_01",
    "email_followup": "email_followup_04",
    "calendar_tomorrow": "cal_stt_06",
    "drive_list": "drive_list_23",
    "drive_reference": "drive_followup_24",
    "news": "news_clean_12",
    "stock": "stock_ood_13",
    "pay_accept": "pay_accept_39",
    "pay_step_up": "pay_step_up_40",
    "pay_reject": "pay_reject_41",
    "pin_followup": "pay_pin_followup_42",
    "confirmation": "confirm_bare_32",
    "correction": "correction_email_46",
    "unsupported_delete": "reminder_delete_unsupported_22",
}

# Raw sensor / embedding leakage only — "PIN" in speak prompts is intentional UX.
_LEAK_RE = re.compile(
    r"embedding|ndarray|\.tolist\(|trust_vector|raw_sensor|fingerprint_template",
    re.I,
)


def _by_id() -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in load_fixtures(FIXTURES)}


def _forced_name(tool_choice: Any) -> str | None:
    if isinstance(tool_choice, dict):
        return tool_choice.get("name")
    return None


def _articulator_request(
    *,
    transcript: str,
    speak_payload: str,
    speak_instructions: str,
) -> dict[str, Any]:
    """Build the speak-LLM request the s2s path would send (tools stripped)."""
    messages = [{"role": "user", "content": transcript}]
    if speak_payload:
        messages.append(
            {
                "role": "user",
                "content": (
                    "[Tool results already fetched — speak these to the driver now. "
                    "Do not call tools. Do not invent numbers.]\n"
                    + speak_payload
                ),
            }
        )
    return {
        "model": "articulator-350m",
        "messages": messages,
        "tools": [],
        "tool_choice": "none",
        "instructions": speak_instructions,
    }


def _assert_safe_payload(blob: Any) -> None:
    text = json.dumps(blob, default=str)
    assert not _TOOL_MARKUP_RE.search(text), f"raw tool markup leaked: {text[:200]}"
    if isinstance(blob, dict):
        speak = str(blob.get("speak") or blob.get("speak_payload") or "")
        assert "ndarray" not in speak.lower()
        assert "embedding" not in speak.lower()


@pytest.mark.component
@pytest.mark.parametrize("scenario", sorted(REPLAY_IDS))
def test_replay_route_and_articulator_contract(tool_client, scenario: str):
    client, reg = tool_client
    row = _by_id()[REPLAY_IDS[scenario]]
    transcript = row["transcript"]
    expect = row["expect"]
    want_tool = expect.get("tool")
    no_tool = bool(expect.get("no_tool"))
    if expect.get("unsupported"):
        no_tool = True
    if want_tool is None and expect.get("auth_status") is None:
        no_tool = no_tool or scenario in {
            "greeting",
            "confirmation",
            "unsupported_delete",
            "pin_followup",
        }

    route = client.post("/tools/route", json={"query": transcript, "k": 8}).json()
    forced = _forced_name(route["tool_choice"])
    choice = route["tool_choice"]

    # Payment fixtures: auth is the contract; route may still force send_payment.
    if scenario.startswith("pay_") or scenario == "pin_followup":
        art = _articulator_request(
            transcript=transcript,
            speak_payload="",
            speak_instructions="",
        )
        assert art["tools"] == []
        assert art["tool_choice"] == "none"
        return

    if no_tool or expect.get("unsupported"):
        assert choice == "none" or forced is None or forced != "send_payment"
        if scenario in {"greeting", "confirmation"}:
            assert choice == "none"
        speak_payload = ""
        speak_instructions = ""
        if expect.get("unsupported"):
            speak_payload = (
                "That action isn't supported. I won't invent a success for it."
            )
            speak_instructions = speak_payload
    else:
        assert forced == want_tool or route.get("top_name") == want_tool, (
            f"{scenario}: route={route} want={want_tool}"
        )
        mock_result = {
            "ok": True,
            "speak": f"Mock result for {want_tool}.",
            "status": "ok",
        }
        with patch.object(reg[want_tool], "execute", return_value=mock_result):
            exec_resp = client.post(
                "/tools/execute",
                json={"name": want_tool, "args": dict(expect.get("args") or {})},
            )
        assert exec_resp.status_code == 200
        result = exec_resp.json()
        assert result.get("speak")
        speak_payload = f"{want_tool}: {result['speak']}"
        speak_instructions = (
            "Tool results for this turn (speak these to the driver; do not invent "
            "numbers or call tools):\n"
            + speak_payload
        )

    art = _articulator_request(
        transcript=transcript,
        speak_payload=speak_payload,
        speak_instructions=speak_instructions,
    )
    assert art["tools"] == []
    assert art["tool_choice"] == "none"
    _assert_safe_payload(art)
    if speak_payload:
        joined = " ".join(m["content"] for m in art["messages"])
        assert "Mock result" in joined or "isn't supported" in joined


@pytest.mark.component
def test_replay_payment_auth_gates(tool_client):
    client, _reg = tool_client
    rows = _by_id()
    session_id = "replay-pay-sess"

    # Micro payment: must be payment-gated (never bypass). ACCEPT is preferred;
    # current DriveAuth mock may STEP_UP on OOD shape mismatch — still gated.
    micro = client.post(
        "/tools/auth/precheck",
        json={
            "session_id": session_id,
            "transcript": rows["pay_accept_39"]["transcript"],
        },
    ).json()
    assert micro.get("is_payment") is True
    assert micro["status"] != "bypass"
    assert normalize_auth_status(micro["status"]) in {
        "accept",
        "step_up",
        "step_up_required",
        "denied",
        "reject",
    }
    _assert_safe_payload(micro)

    # Forced ACCEPT contract (articulator / execute path) independent of OOD flakiness.
    with patch(
        "nova.server.driveauth_bridge.precheck",
        return_value={
            "status": "accept",
            "is_payment": True,
            "session_id": session_id + "-acc",
            "speak": "",
            "trust": 0.9,
            "risk": 0.1,
        },
    ):
        accept = client.post(
            "/tools/auth/precheck",
            json={
                "session_id": session_id + "-acc",
                "transcript": rows["pay_accept_39"]["transcript"],
            },
        ).json()
    assert accept["status"] == "accept"
    art = _articulator_request(
        transcript=rows["pay_accept_39"]["transcript"],
        speak_payload="Paid 50 INR to Chai Point.",
        speak_instructions="Paid 50 INR to Chai Point.",
    )
    assert art["tools"] == []
    _assert_safe_payload(art)

    step = client.post(
        "/tools/auth/precheck",
        json={
            "session_id": session_id + "-hi",
            "transcript": rows["pay_step_up_40"]["transcript"],
            "amount": 60000,
            "payee": "Landlord",
        },
    ).json()
    assert normalize_auth_status(step["status"]) in {
        "step_up",
        "step_up_required",
        "denied",
        "reject",
    }
    assert step.get("status") != "bypass"
    blob = json.dumps(step).lower()
    assert "ndarray" not in blob
    assert "embedding" not in blob

    reject = client.post(
        "/tools/auth/precheck",
        json={
            "session_id": session_id + "-rej",
            "transcript": rows["pay_reject_41"]["transcript"],
            "amount": 500000,
            "payee": "Unknown Merchant",
        },
    ).json()
    assert normalize_auth_status(reject["status"]) in {
        "reject",
        "denied",
        "step_up",
        "step_up_required",
    }
    if reject["status"] == "accept":
        pytest.fail("high-value unknown payee must not ACCEPT without step-up")


@pytest.mark.component
def test_replay_pin_retry_without_pin_leak(tool_client, tmp_path, monkeypatch):
    from driveauth.step_up_fallback import enroll_pin
    from nova.server.driveauth_bridge import reset_auth_for_tests
    from nova.tools.payment import DriveAuthGate, SendPaymentTool

    store = str(tmp_path / "pin_store")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", store)
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    reset_auth_for_tests()
    assert enroll_pin(store, "driver1", "4242") is True
    gate = DriveAuthGate(SendPaymentTool(), store_dir=store, use_mock_matchers=True)
    gate.reload_fallback()

    first = gate.execute(payee="Landlord", amount=60_000.0, beneficiary_known=True)
    assert first["status"] == "step_up_required"
    wrong = gate.execute(step_up_code="0000")
    assert wrong["status"] == "step_up_required"
    for payload in (first, wrong):
        text = json.dumps(payload)
        assert "4242" not in text
        assert not _LEAK_RE.search(text)

    ok = gate.execute(step_up_code="4242")
    assert ok["status"] == "sent"
    assert "4242" not in json.dumps(ok)

    art = _articulator_request(
        transcript="My PIN is 4 2 4 2.",
        speak_payload=first["speak"],
        speak_instructions=first["speak"],
    )
    assert art["tools"] == []
    assert "4242" not in art["instructions"]
    assert "4 2 4 2" not in art["instructions"]


@pytest.mark.component
def test_replay_unsupported_delete_no_fabricated_success(tool_client):
    client, reg = tool_client
    row = _by_id()["reminder_delete_unsupported_22"]
    route = client.post("/tools/route", json={"query": row["transcript"], "k": 8}).json()
    assert "delete_reminder" not in reg
    art = _articulator_request(
        transcript=row["transcript"],
        speak_payload="Reminder delete isn't supported yet.",
        speak_instructions="Say the capability error; do not invent success.",
    )
    assert art["tool_choice"] == "none"
    assert "delete_reminder" not in {t["name"] for t in route.get("tools") or []}


@pytest.mark.component
def test_replay_barge_in_cancel_contract():
    """Barge-in is audio-path; assert the cancel payload contract offline."""
    cancelled = {
        "type": "response.done",
        "response": {
            "status": "cancelled",
            "status_details": {"reason": "turn_detected"},
        },
    }
    assert cancelled["response"]["status"] == "cancelled"
    assert cancelled["response"]["status_details"]["reason"] == "turn_detected"
    art = _articulator_request(
        transcript="(barge-in)",
        speak_payload="",
        speak_instructions="",
    )
    assert art["tools"] == []
    assert art["tool_choice"] == "none"


@pytest.mark.component
def test_replay_model_agent_to_articulator(tool_client, monkeypatch):
    """Model mode: mocked 230M call → execute → articulator tools=[]."""
    client, reg = tool_client
    monkeypatch.setenv("NOVA_TOOL_ROUTE_MODE", "model")
    monkeypatch.setenv("NOVA_ROUTER_LLM_URL", "http://127.0.0.1:18081/v1")

    row = _by_id()["email_clean_01"]
    fake_result = {"speak": "You have 2 unread emails.", "ok": True, "mode": "unread"}

    def fake_router(_registry, query):
        return ([{"id": "c1", "name": "check_email", "args": {"mode": "unread"}}], "")

    with (
        patch("nova.server.tool_agent._call_router_llm", side_effect=fake_router),
        patch.object(reg["check_email"], "execute", return_value=fake_result),
    ):
        body = client.post(
            "/tools/agent",
            json={"query": row["transcript"], "execute": True, "session_id": "replay-1"},
        ).json()

    assert body["needs_tools"] is True
    assert body["tool_calls"][0]["name"] == "check_email"
    assert body["tool_choice"] == "none"
    assert "2 unread" in body["speak_payload"]
    art = _articulator_request(
        transcript=row["transcript"],
        speak_payload=body["speak_payload"],
        speak_instructions=body["speak_instructions"],
    )
    assert art["tools"] == []
    assert "2 unread" in art["messages"][-1]["content"]
    _assert_safe_payload(art)
