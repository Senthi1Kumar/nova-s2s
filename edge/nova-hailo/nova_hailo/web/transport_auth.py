"""Transport authentication for the HTTP/WS surface.

Distinct from nova_hailo/auth.py, which decides whether a *tool call* needs
step-up confirmation. This module decides whether a *connection* may talk to
the server at all.

Why loopback is trusted and the network is not
----------------------------------------------
run_web.sh binds 0.0.0.0 so the operator can open the dashboard from a laptop,
which also puts /integrations/connectors, /integrations/google/auth/start and
the realtime socket in front of everything else on the network. The HMI and a
browser on the Pi arrive on loopback, and reaching loopback already requires
code execution on the device -- an attacker who has that does not need the
HTTP API. Everything else presents a token or is refused.

Fail closed, deliberately
-------------------------
A non-loopback request with no token *configured* is denied, not allowed. The
alternative -- treating "no token set" as "auth disabled" -- makes the safe
state the one you have to remember to switch on, and the failure silent. Here
the failure is a 401 naming the env var that is missing.

Browsers and the WebSocket
--------------------------
The browser WebSocket API cannot set request headers, so a token may also
arrive as a ?token= query parameter. That is a concession to the platform, not
a convenience: query strings land in access logs and Referer headers, so the
query form is accepted only where the header is impossible.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from enum import Enum

TOKEN_ENV = "NOVA_HAILO_API_TOKEN"


class TransportDecision(str, Enum):
    ALLOW_LOOPBACK = "allow_loopback"
    ALLOW_TOKEN = "allow_token"
    DENY_NO_TOKEN_CONFIGURED = "deny_no_token_configured"
    DENY_BAD_TOKEN = "deny_bad_token"


@dataclass(frozen=True)
class TransportAuthResult:
    decision: TransportDecision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision in {
            TransportDecision.ALLOW_LOOPBACK,
            TransportDecision.ALLOW_TOKEN,
        }


def is_loopback(host: str | None) -> bool:
    """True only for addresses that provably resolve to this machine.

    Anything unparseable is not loopback. A hostname we cannot resolve to a
    loopback literal must not be trusted just because it looks local -- "is
    this the local machine" is answered by the address, not the label.
    """
    if not host:
        return False
    h = host.strip()
    if not h:
        return False
    if h == "localhost":
        return True
    # Tolerate the bracketed and zoned IPv6 forms uvicorn can report.
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def bearer_token(authorization: str | None) -> str | None:
    """Extract the credential from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def configured_token(env: dict[str, str] | None = None) -> str | None:
    src = os.environ if env is None else env
    tok = (src.get(TOKEN_ENV) or "").strip()
    return tok or None


def authorize(
    *,
    client_host: str | None,
    authorization: str | None = None,
    query_token: str | None = None,
    token: str | None = None,
) -> TransportAuthResult:
    """Decide whether one connection may proceed.

    `token` is the configured secret; None means none is configured.
    """
    if is_loopback(client_host):
        return TransportAuthResult(TransportDecision.ALLOW_LOOPBACK, "loopback client")

    if not token:
        return TransportAuthResult(
            TransportDecision.DENY_NO_TOKEN_CONFIGURED,
            f"{TOKEN_ENV} is not set; remote clients are refused. Set it, or "
            "bind the server to 127.0.0.1 only.",
        )

    presented = bearer_token(authorization) or (query_token or "").strip() or None
    if presented is not None and hmac.compare_digest(presented, token):
        return TransportAuthResult(TransportDecision.ALLOW_TOKEN, "valid token")

    return TransportAuthResult(
        TransportDecision.DENY_BAD_TOKEN, "missing or invalid bearer token"
    )
