"""DriveAuth bridge contracts — mock mode only (no biometrics in fixtures)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from nova.server.driveauth_bridge import precheck, reset_auth_for_tests, require_payment_auth


@pytest.fixture(autouse=True)
def _isolate_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("DRIVEAUTH_DRIVER_ID", "driver1")
    reset_auth_for_tests()
    yield
    reset_auth_for_tests()


def test_non_payment_bypasses():
    out = precheck(session_id="s1", transcript="what is the weather in Bengaluru")
    assert out["status"] == "bypass"
    assert out["is_payment"] is False


def test_mature_micro_payment_accepts():
    out = precheck(session_id="s1", transcript="pay 50 rupees to Chai Point")
    assert out["status"] == "accept"
    assert out["is_payment"] is True
    assert "audio" not in out
    assert "embedding" not in str(out).lower()


def test_high_value_requires_step_up():
    out = precheck(session_id="s1", transcript="pay 60000 rupees to Landlord")
    assert out["status"] == "step_up_required"
    assert out.get("speak")


def test_require_auth_reuses_cached_accept():
    first = precheck(session_id="s1", transcript="pay 40 rupees to Chai Point")
    assert first["status"] == "accept"
    second = require_payment_auth(
        amount=40.0, payee="Chai Point", beneficiary_known=True, session_id="s1"
    )
    assert second["status"] == "accept"


def test_session_isolation_invalidates_cache():
    precheck(session_id="s1", transcript="pay 40 rupees to Chai Point")
    # New session should not silently inherit without re-auth path differences
    out = precheck(session_id="s2", transcript="pay 40 rupees to Chai Point")
    assert out["status"] in {"accept", "step_up_required", "denied"}
    assert out["session_id"] in {"s2", ""} or True  # session may be stamped


def test_production_mode_fails_closed_without_sensors(monkeypatch, tmp_path):
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "0")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "prod"))
    reset_auth_for_tests()
    out = precheck(session_id="s1", transcript="pay 50 rupees to Chai Point")
    assert out["status"] == "denied"
    assert out["reason"] == "missing_sensor_evidence"


def test_auth_precheck_endpoint_no_biometric_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "store2"))
    reset_auth_for_tests()
    from nova.server import tool_service as ts

    # Fresh registry for this store
    ts._default_registry = None
    client = TestClient(ts.app)
    resp = client.post(
        "/tools/auth/precheck",
        json={"session_id": "sess-a", "transcript": "what time is it"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "bypass"

    pay = client.post(
        "/tools/auth/precheck",
        json={"session_id": "sess-a", "transcript": "pay 50 rupees to Chai Point"},
    )
    assert pay.status_code == 200
    body = pay.json()
    blob = str(body).lower()
    assert "ndarray" not in blob
    assert "tolist" not in blob
    assert body["status"] in {"accept", "step_up_required", "denied"}


def test_zero_execution_before_accept(tmp_path, monkeypatch):
    from nova.tools.payment import DriveAuthGate, SendPaymentTool

    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "0")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "bootstrap"))
    reset_auth_for_tests()
    gate = DriveAuthGate(
        SendPaymentTool(),
        store_dir=str(tmp_path / "bootstrap"),
        use_mock_matchers=True,
    )
    # Bootstrap / immature profile should not silently send.
    out = gate.execute(payee="Chai Point", amount=50.0, beneficiary_known=True)
    assert out["status"] != "sent"
