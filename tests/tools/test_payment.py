"""Tests for nova.tools.payment — DriveAuth-gated simulated payments."""


def test_driveauth_package_importable():
    from driveauth import DriveAuth
    from driveauth.types import Decision

    assert hasattr(DriveAuth, "load")
    assert Decision.ACCEPT.legacy() == "pass"


def test_send_payment_returns_sent_with_txn_id():
    from nova.tools.payment import SendPaymentTool

    tool = SendPaymentTool()
    out = tool.execute(payee="Alice", amount=120.0)
    assert out["status"] == "sent"
    assert out["payee"] == "Alice"
    assert out["amount"] == 120.0
    assert out["currency"] == "INR"
    assert len(out["txn_id"]) == 12


def test_send_payment_schema_is_well_formed():
    from nova.tools.payment import SendPaymentTool

    tool = SendPaymentTool()
    ft = tool.to_function_tool()
    assert ft["type"] == "function"
    assert ft["name"] == "send_payment"
    assert set(ft["parameters"]["required"]) == {"payee", "amount"}


import pytest

from nova.tools.payment import DriveAuthGate, SendPaymentTool


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    from nova.server.driveauth_bridge import reset_auth_for_tests

    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "store"))
    reset_auth_for_tests()
    return DriveAuthGate(
        SendPaymentTool(),
        store_dir=str(tmp_path / "store"),
        use_mock_matchers=True,
    )


def test_micro_known_payee_accepts_and_executes(gate):
    out = gate.execute(payee="Chai Point", amount=50.0, beneficiary_known=True)
    assert out["status"] == "sent"
    assert out["auth"]["decision"] == "accept"
    assert 0.0 <= out["auth"]["trust"] <= 1.0


def test_high_value_ladder_accepts_on_strong_mock(gate):
    """phase_7+: high_value clears via Voice→Face→Finger ladder (no mandatory OTP)."""
    out = gate.execute(payee="Chai Point", amount=60_000.0, beneficiary_known=True)
    assert out["status"] == "sent"
    assert out["auth"]["decision"] == "accept"


def test_step_up_completes_with_correct_pin(tmp_path, monkeypatch):
    """Nova-owned PIN resume path (guest/OTP still yields step_up_required upstream)."""
    from driveauth.step_up_fallback import enroll_pin
    from nova.server.driveauth_bridge import reset_auth_for_tests

    store = str(tmp_path / "store")
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", store)
    reset_auth_for_tests()
    gate = DriveAuthGate(SendPaymentTool(), store_dir=store, use_mock_matchers=True)
    assert enroll_pin(store, "driver1", "4321") is True
    gate.reload_fallback()  # pick up the just-enrolled PIN

    # Seed pending as if DriveAuth returned STEP_UP (guest / OTP path).
    gate._pending = {
        "payee": "Landlord",
        "amount": 60_000.0,
        "beneficiary_known": True,
    }
    gate._retries = 0

    second = gate.execute(step_up_code="4321")
    assert second["status"] == "sent"
    assert second["payee"] == "Landlord"
    assert second["auth"]["method"] == "step_up"


def test_step_up_exhausts_after_retries(tmp_path, monkeypatch):
    from nova.server.driveauth_bridge import reset_auth_for_tests

    store = str(tmp_path / "store")
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", store)
    reset_auth_for_tests()
    gate = DriveAuthGate(SendPaymentTool(), store_dir=store, use_mock_matchers=True)
    gate._pending = {
        "payee": "Landlord",
        "amount": 60_000.0,
        "beneficiary_known": True,
    }
    gate._retries = 0

    last: dict = {}
    for _ in range(5):  # more than STEP_UP_RETRIES wrong codes
        last = gate.execute(step_up_code="0000")
        if last["status"] == "denied":
            break
    assert last["status"] == "denied"
    assert last["reason"] == "step_up_exhausted"


def test_unenrolled_real_matchers_never_accept(tmp_path, monkeypatch):
    """Production execute without sensor-backed precheck must fail closed."""
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "0")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "store"))
    from nova.server.driveauth_bridge import reset_auth_for_tests

    reset_auth_for_tests()
    gate = DriveAuthGate(
        SendPaymentTool(), store_dir=str(tmp_path / "store"), use_mock_matchers=False
    )
    out = gate.execute(payee="Chai Point", amount=50.0, beneficiary_known=True)
    assert out["status"] in ("denied", "step_up_required")  # never silently "sent"
    assert out.get("reason") == "missing_sensor_evidence"
