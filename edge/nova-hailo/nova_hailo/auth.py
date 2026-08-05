from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class AuthDecision(str, Enum):
    BYPASS = "bypass"
    ACCEPT = "accept"
    STEP_UP = "step_up_required"
    DENIED = "denied"


@dataclass
class AuthResult:
    decision: AuthDecision
    speak: str | None = None
    is_payment: bool = False


class DriveAuthPrecheck:
    """Nova-style payment gate. Swap body for real Drive_auth_edge HTTP later."""

    def __init__(
        self,
        enabled: bool = True,
        payment_keywords: list[str] | None = None,
        confirm_keywords: list[str] | None = None,
        deny_keywords: list[str] | None = None,
    ):
        self.enabled = enabled
        self.payment_keywords = [k.lower() for k in (payment_keywords or [])]
        self.confirm_keywords = [k.lower() for k in (confirm_keywords or [])]
        self.deny_keywords = [k.lower() for k in (deny_keywords or [])]
        self._session_accepted = False

    def reset_session(self):
        self._session_accepted = False

    def _match_any(self, text: str, keywords: list[str]) -> bool:
        t = text.lower()
        return any(k in t for k in keywords)

    def precheck(self, transcript: str, session_id: str = "default") -> AuthResult:
        if not self.enabled:
            return AuthResult(decision=AuthDecision.BYPASS)

        text = (transcript or "").strip()
        if not text:
            return AuthResult(decision=AuthDecision.BYPASS)

        if self._match_any(text, self.deny_keywords):
            self._session_accepted = False
            return AuthResult(
                decision=AuthDecision.DENIED,
                speak="Payment cancelled.",
                is_payment=True,
            )

        is_payment = self._match_any(text, self.payment_keywords)
        if not is_payment:
            return AuthResult(decision=AuthDecision.BYPASS)

        if self._session_accepted or self._match_any(text, self.confirm_keywords):
            self._session_accepted = True
            return AuthResult(decision=AuthDecision.ACCEPT, is_payment=True)

        return AuthResult(
            decision=AuthDecision.STEP_UP,
            speak="This looks like a payment. Say confirm to authorize, or cancel payment to abort.",
            is_payment=True,
        )


def looks_like_payment_tool(name: str) -> bool:
    return bool(re.search(r"pay|payment|transfer", name or "", re.I))
