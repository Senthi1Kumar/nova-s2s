"""Process-wide DriveAuth owner for the Nova tool service (mock-first).

Single owner for precheck, require_auth cache, OTP/PIN pending state, and
audit. s2s and /tools/execute must not hold separate DriveAuth instances.

LiteRT paths are frozen — this module never imports nova.engine.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from driveauth import DriveAuth
from driveauth.intent import is_payment_utterance, parse_transaction_intent
from driveauth.types import Decision

logger = logging.getLogger("nova.server.driveauth_bridge")

_auth: DriveAuth | None = None


def _mock_enabled() -> bool:
    return os.getenv("DRIVEAUTH_USE_MOCK", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def store_dir() -> Path:
    raw = os.getenv("DRIVEAUTH_STORE_DIR") or str(Path("runtime") / "driveauth_store")
    return Path(raw)


def driver_id() -> str:
    return os.getenv("DRIVEAUTH_DRIVER_ID", "driver1")


def mock_audio() -> np.ndarray:
    """Synthetic audio for mock matchers only — never used in production mode."""
    seconds, sr = 1.5, 16_000
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, dtype=np.float32)
    rng = np.random.default_rng(0)
    envelope = 0.05 + 0.15 * (0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t))
    speech = envelope * np.sin(2 * np.pi * 180 * t)
    noise = 0.005 * rng.standard_normal(n).astype(np.float32)
    return (speech + noise).astype(np.float32)



def _seed_demo_beneficiaries(auth: DriveAuth) -> None:
    """Known payees for mock demos so micro payments classify as micro, not high_value."""
    path = Path(auth._store) / "beneficiaries" / f"{auth.driver_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [
        "Chai Point",
        "Mom",
        "Landlord",
        "Amazon",
        "Uber",
        "Swiggy",
    ]
    existing = set()
    if path.exists():
        existing = {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}
    merged = sorted(existing | set(names))
    path.write_text("\n".join(merged) + "\n")


def get_auth() -> DriveAuth:
    """Lazy singleton. Mock demos auto-mature the profile so micro payments ACCEPT."""
    global _auth
    if _auth is not None:
        return _auth
    mock = _mock_enabled()
    path = store_dir()
    path.mkdir(parents=True, exist_ok=True)
    auth = DriveAuth.load(
        store_dir=str(path),
        enroll_dir=os.getenv("DRIVEAUTH_ENROLL_DIR"),
        driver_id=driver_id(),
        use_mock_matchers=mock,
    )
    if mock:
        # Keep OOD baselines aligned with MockFaceMatcher (MOCK_FACE_DIM=512).
        # A stale 128-d reseed → shape mismatch → fail-closed STEP_UP.
        try:
            from driveauth.matchers.mock import MOCK_FACE_DIM
            from driveauth.ood_detector import OODDetector

            auth._engine._ood = OODDetector.seed_baselines(
                str(path),
                driver_id(),
                voice_dim=192,
                face_dim=MOCK_FACE_DIM,
                finger_dim=64,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DriveAuth mock OOD reseed failed: %s", exc)
    if mock and os.getenv("DRIVEAUTH_SEED_MATURE", "1").strip() not in {
        "0",
        "false",
        "off",
        "no",
    }:
        try:
            auth._profile.seed_mature()
            _seed_demo_beneficiaries(auth)
            logger.info("DriveAuth mock store matured for demo (%s)", path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DriveAuth mature seed failed: %s", exc)
    _auth = auth
    return auth


def reset_auth_for_tests() -> None:
    """Drop singleton (tests only)."""
    global _auth
    _auth = None


def _normalize(result: Any) -> dict[str, Any]:
    decision = result.decision
    if decision is Decision.ACCEPT:
        status = "accept"
    elif decision is Decision.STEP_UP_REQUIRED:
        status = "step_up_required"
    else:
        status = "denied"
    out: dict[str, Any] = {
        "status": status,
        "decision": decision.value if hasattr(decision, "value") else str(decision),
        "trust": round(float(result.trust_score), 3),
        "risk": round(float(result.risk_score), 3),
        "tier": getattr(result, "tier", "") or "",
        "rule": getattr(result, "policy_rule", "") or "",
        "session_id": getattr(result, "session_id", "") or "",
        "explanations": list(getattr(result, "explanations", []) or [])[:5],
        # Never include embeddings / raw audio.
    }
    if status == "step_up_required":
        method = getattr(result, "step_up_method", None) or "pin"
        out["method"] = method
        if method == "otp_mobile":
            out["prompt"] = (
                "A one-time code was sent to the driver's registered mobile. "
                "Ask them to read it out."
            )
        else:
            out["prompt"] = (
                "Extra verification needed. Ask the driver to say their payment PIN."
            )
        out["speak"] = out["prompt"]
    elif status == "denied":
        out["speak"] = "I couldn't verify your identity for that payment. Stopping."
        out["reason"] = out["rule"] or "driveauth_reject"
    else:
        out["speak"] = ""
    return out


def precheck(
    *,
    session_id: str,
    transcript: str,
    amount: float | None = None,
    payee: str | None = None,
) -> dict[str, Any]:
    """First-layer payment authorization before tool routing.

    Non-payment transcripts return ``{"status": "bypass"}``.
    """
    q = (transcript or "").strip()
    if not q or not is_payment_utterance(q):
        return {"status": "bypass", "is_payment": False, "session_id": session_id}

    auth = get_auth()
    if session_id:
        # Align DriveAuth session with realtime connection when provided.
        if auth.session_id != session_id:
            auth._session_id = session_id
            auth.invalidate_cache()

    intent = parse_transaction_intent(q, channel="voice")
    amt = float(amount) if amount is not None else float(intent.amount or 0.0)
    beneficiary = (payee or intent.beneficiary or "").strip()
    known = False
    if beneficiary:
        known = auth._is_known_beneficiary(beneficiary)

    if _mock_enabled():
        audio = mock_audio()
    else:
        # Production without sensor evidence must fail closed — never synthesize.
        return {
            "status": "denied",
            "decision": "REJECT",
            "trust": 0.0,
            "risk": 1.0,
            "tier": "payment",
            "rule": "missing_sensor_evidence",
            "session_id": session_id,
            "explanations": ["production mode requires real sensor evidence"],
            "speak": "I couldn't verify your identity for that payment. Stopping.",
            "reason": "missing_sensor_evidence",
            "is_payment": True,
            "amount": float(amount) if amount is not None else 0.0,
            "payee": (payee or "").strip(),
            "currency": "INR",
            "action": "pay",
        }

    result = auth.authenticate(
        audio_np=audio,
        tier_hint="payment",
        amount=amt,
        beneficiary=beneficiary,
        action=intent.action or "pay",
        currency=intent.currency or "INR",
        channel="voice",
        beneficiary_known=known,
        is_payment=True,
        voice_expected=True,
        session_id=session_id or None,
        event="nova_precheck",
        transcript=q,
    )
    out = _normalize(result)
    out["is_payment"] = True
    out["amount"] = amt
    out["payee"] = beneficiary
    out["currency"] = intent.currency or "INR"
    out["action"] = intent.action or "pay"
    return out


def require_payment_auth(
    *,
    amount: float,
    payee: str = "",
    beneficiary_known: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Second-layer gate at send_payment execute (may reuse cached ACCEPT).

    In production (``DRIVEAUTH_USE_MOCK=0``), only a fresh sensor-backed
    precheck ACCEPT may authorize. DriveAuth's per-modality mock fallbacks
    must never silently ACCEPT an unenrolled store.
    """
    auth = get_auth()
    if session_id and auth.session_id != session_id:
        auth._session_id = session_id

    if not _mock_enabled():
        if auth._can_reuse_cached(float(amount), bool(beneficiary_known)):
            return _normalize(auth._last_result)
        return {
            "status": "denied",
            "decision": "REJECT",
            "trust": 0.0,
            "risk": 1.0,
            "tier": "payment",
            "rule": "missing_sensor_evidence",
            "session_id": session_id or auth.session_id,
            "explanations": ["production execute requires prior sensor-backed ACCEPT"],
            "speak": "I couldn't verify your identity for that payment. Stopping.",
            "reason": "missing_sensor_evidence",
        }

    result = auth.require_auth(
        tier="payment",
        amount=float(amount),
        beneficiary=payee,
        action="send_payment",
        channel="llm_tool",
        beneficiary_known=beneficiary_known,
        allow_cached=True,
    )
    return _normalize(result)
