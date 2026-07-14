"""Simulated in-vehicle payments, gated by DriveAuth Trust/Risk authorization.

``SendPaymentTool`` is simulated (no real money). ``DriveAuthGate`` wraps it
with the Drive_auth_edge pipeline via ``nova.server.driveauth_bridge`` — the
process-wide auth owner used by both precheck and tool execute.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from driveauth import config as da_config
from driveauth.step_up_fallback import StepUpFallback
from driveauth.step_up_otp import OTPStepUp

from nova.server.driveauth_bridge import (
    get_auth,
    mock_audio,
    require_payment_auth,
    reset_auth_for_tests,
    store_dir as default_store_dir,
    _mock_enabled,
)
from nova.tools.base import NovaTool

logger = logging.getLogger("nova.tools.payment")


class SendPaymentTool(NovaTool):
    """Send a simulated payment to a named payee."""

    name = "send_payment"
    description = (
        "Send a payment to a named payee (simulated — no real money moves). "
        "Use when the driver asks to pay, buy, order, or transfer money."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "payee": {"type": "string", "description": "Who to pay (merchant or person)."},
            "amount": {"type": "number", "description": "Amount to pay."},
            "currency": {"type": "string", "description": "ISO currency code.", "default": "INR"},
            "beneficiary_known": {
                "type": "boolean",
                "description": "True if the driver has paid this payee before.",
            },
        },
        "required": ["payee", "amount"],
    }

    def execute(
        self,
        payee: str,
        amount: float,
        currency: str = "INR",
        beneficiary_known: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "status": "sent",
            "txn_id": uuid.uuid4().hex[:12],
            "payee": payee,
            "amount": float(amount),
            "currency": currency,
            "speak": f"Paid {float(amount):.0f} {currency} to {payee}.",
        }


class DriveAuthGate(NovaTool):
    """Wraps a payment tool behind DriveAuth (Trust/Risk + step-up).

    Uses the shared ``driveauth_bridge`` singleton so precheck and execute share
    decision cache / fraud / session state. Never fabricates audio outside mock mode.
    """

    def __init__(
        self,
        tool: NovaTool,
        *,
        store_dir: str | None = None,
        driver_id: str = "driver1",
        use_mock_matchers: bool | None = None,
    ):
        self._tool = tool
        self.name = tool.name
        self.description = (
            tool.description
            + " Protected by biometric authorization; may require a spoken PIN or one-time code."
        )
        self.parameters = {
            **tool.parameters,
            "properties": {
                **tool.parameters.get("properties", {}),
                "step_up_code": {
                    "type": "string",
                    "description": (
                        "The PIN or one-time code the driver read out, ONLY when the "
                        "previous send_payment call returned step_up_required."
                    ),
                },
            },
        }
        if store_dir:
            os.environ["DRIVEAUTH_STORE_DIR"] = str(store_dir)
        if driver_id:
            os.environ["DRIVEAUTH_DRIVER_ID"] = driver_id
        if use_mock_matchers is not None:
            os.environ["DRIVEAUTH_USE_MOCK"] = "1" if use_mock_matchers else "0"
            # Fresh store for each gate instance in tests.
            reset_auth_for_tests()
        self._otp = OTPStepUp()
        self._fallback = StepUpFallback(
            str(os.getenv("DRIVEAUTH_STORE_DIR") or default_store_dir()),
            os.getenv("DRIVEAUTH_DRIVER_ID", "driver1"),
        )
        self._pending: dict[str, Any] | None = None
        self._retries = 0
        get_auth()

    def reload_fallback(self) -> None:
        self._fallback = StepUpFallback(
            str(os.getenv("DRIVEAUTH_STORE_DIR") or default_store_dir()),
            os.getenv("DRIVEAUTH_DRIVER_ID", "driver1"),
        )

    def execute(self, step_up_code: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if self._pending is not None:
            return self._resume(step_up_code)

        amount = float(kwargs.get("amount", 0.0))
        payee = str(kwargs.get("payee", ""))
        known = bool(kwargs.get("beneficiary_known", False))
        session_id = kwargs.pop("session_id", None)

        gate = require_payment_auth(
            amount=amount,
            payee=payee,
            beneficiary_known=known,
            session_id=session_id,
        )
        logger.info(
            "DriveAuthGate: %s trust=%.3f risk=%.3f tier=%s",
            gate.get("decision"),
            float(gate.get("trust") or 0),
            float(gate.get("risk") or 0),
            gate.get("tier"),
        )

        if gate["status"] == "accept":
            out = self._tool.execute(**kwargs)
            out["auth"] = {
                "decision": "accept",
                "trust": gate.get("trust"),
                "risk": gate.get("risk"),
                "rule": gate.get("rule"),
            }
            return out

        if gate["status"] == "step_up_required":
            self._pending = dict(kwargs)
            self._retries = 0
            mobile = os.getenv("DRIVEAUTH_DRIVER_MOBILE")
            method = gate.get("method") or "pin"
            if method == "otp_mobile" and self._otp.send(mobile) is not None:
                prompt = (
                    "A one-time code was sent to the driver's registered mobile. "
                    "Ask them to read it out, then call send_payment again with step_up_code."
                )
                method = "otp_mobile"
            else:
                method, prompt = "pin", (
                    "Extra verification needed. Ask the driver to say their payment PIN, "
                    "then call send_payment again with step_up_code."
                )
            return {
                "status": "step_up_required",
                "method": method,
                "prompt": prompt,
                "speak": prompt,
                "tier": gate.get("tier"),
            }

        return {
            "status": "denied",
            "reason": gate.get("reason") or gate.get("rule") or "driveauth_reject",
            "explanations": gate.get("explanations") or [],
            "speak": gate.get("speak")
            or "I couldn't verify your identity for that payment. Stopping.",
        }

    def _resume(self, step_up_code: str | None) -> dict[str, Any]:
        if not step_up_code:
            return {
                "status": "step_up_required",
                "method": "pin",
                "prompt": "Still waiting for the driver's PIN or one-time code.",
                "speak": "Still waiting for the driver's PIN or one-time code.",
            }
        digits = "".join(ch for ch in str(step_up_code) if ch.isdigit())

        if self._otp.has_active_challenge:
            passed = self._otp.verify(digits)
        else:
            auth = get_auth()
            audio = mock_audio() if _mock_enabled() else None
            passed, _reasons = self._fallback.run(
                pin=digits,
                biometric_recheck=lambda: auth.authenticate(
                    audio_np=audio, tier_hint="payment", audit=False, is_payment=False
                ).trust_score,
            )

        if passed:
            pending, self._pending, self._retries = self._pending, None, 0
            out = self._tool.execute(**pending)
            out["auth"] = {"decision": "accept", "method": "step_up"}
            return out

        self._retries += 1
        if self._retries >= da_config.STEP_UP_RETRIES:
            self._pending, self._retries = None, 0
            return {
                "status": "denied",
                "reason": "step_up_exhausted",
                "explanations": [],
                "speak": "Too many failed verification attempts. Payment cancelled.",
            }
        return {
            "status": "step_up_required",
            "method": "pin",
            "prompt": "That code didn't match. Ask the driver to try once more.",
            "speak": "That code didn't match. Ask the driver to try once more.",
        }
