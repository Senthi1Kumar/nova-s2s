"""Transport auth decisions: loopback trusted, network gated, fail closed.

Callers: pytest. Pure functions from nova_hailo.web.transport_auth -- no
FastAPI, no socket, no Hailo. No date schemas.
"""
from __future__ import annotations

import pytest

from nova_hailo.web.transport_auth import (
    TOKEN_ENV,
    TransportDecision,
    authorize,
    bearer_token,
    configured_token,
    is_loopback,
)

SECRET = "s3cret-token-value"


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.53", "localhost", "::1", "[::1]", "::1%lo0", " 127.0.0.1 "],
)
def test_loopback_forms_are_recognised(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    ["192.168.1.36", "10.0.0.5", "0.0.0.0", "example.com", "", None, "not-an-ip"],
)
def test_non_loopback_and_unparseable_are_not_trusted(host):
    """An unresolvable name must not be trusted for looking local."""
    assert is_loopback(host) is False


def test_loopback_needs_no_credential():
    r = authorize(client_host="127.0.0.1", token=None)
    assert r.allowed is True
    assert r.decision is TransportDecision.ALLOW_LOOPBACK


def test_remote_without_configured_token_is_refused_not_waved_through():
    """"No token set" must mean closed, not "auth disabled"."""
    r = authorize(client_host="192.168.1.36", token=None)
    assert r.allowed is False
    assert r.decision is TransportDecision.DENY_NO_TOKEN_CONFIGURED
    assert TOKEN_ENV in r.reason


def test_remote_with_valid_bearer_header_is_allowed():
    r = authorize(
        client_host="192.168.1.36",
        authorization=f"Bearer {SECRET}",
        token=SECRET,
    )
    assert r.allowed is True
    assert r.decision is TransportDecision.ALLOW_TOKEN


def test_remote_with_valid_query_token_is_allowed():
    """Browser WebSockets cannot set headers; the query form covers them."""
    r = authorize(client_host="192.168.1.36", query_token=SECRET, token=SECRET)
    assert r.allowed is True
    assert r.decision is TransportDecision.ALLOW_TOKEN


@pytest.mark.parametrize(
    "authorization, query",
    [
        (None, None),
        ("Bearer wrong-token", None),
        ("Basic " + SECRET, None),
        (f"Bearer {SECRET}x", None),
        (None, "wrong-token"),
        ("Bearer", None),
        ("", ""),
    ],
)
def test_remote_with_bad_credential_is_denied(authorization, query):
    r = authorize(
        client_host="192.168.1.36",
        authorization=authorization,
        query_token=query,
        token=SECRET,
    )
    assert r.allowed is False
    assert r.decision is TransportDecision.DENY_BAD_TOKEN


@pytest.mark.parametrize(
    "header, expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER   abc  ", "abc"),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer", None),
        ("Bearer   ", None),
        (None, None),
        ("", None),
    ],
)
def test_bearer_token_parsing(header, expected):
    assert bearer_token(header) == expected


def test_configured_token_reads_env_and_treats_blank_as_unset():
    assert configured_token({TOKEN_ENV: SECRET}) == SECRET
    assert configured_token({TOKEN_ENV: "   "}) is None
    assert configured_token({}) is None
