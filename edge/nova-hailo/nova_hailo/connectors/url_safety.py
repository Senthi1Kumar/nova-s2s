"""SSRF guard shared by the connector registry (registration time) and the
MCP client (fetch time, immediately before every outbound request).

Registration-time validation alone is bypassable by DNS rebinding: a
hostname can resolve to a public address at the moment a connector is added
and to a loopback / link-local / private / cloud-metadata address by the
time a tool call actually goes out over the network. So `assert_public_http_url`
must be called again right before every outbound fetch, not just once when
the connector is stored -- `mcp_client._rpc_call` does exactly that.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeConnectorURLError(ValueError):
    """URL is disallowed: bad scheme, no host, unresolvable, or resolves to
    a non-public address. Message is safe to show the user."""


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) must be judged by the
    # IPv4 address it maps to, not by the (public-looking) IPv6 wrapper.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_disallowed_ip(mapped):
        return True
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_http_url(url: str) -> str:
    """Raise UnsafeConnectorURLError unless `url` is http(s) with a host that
    resolves ONLY to public, routable addresses (both IPv4 and IPv6).

    Call this both when a connector is registered AND again immediately
    before every outbound fetch against it -- the fetch-time re-check is the
    one that actually defeats DNS rebinding, since registration-time-only
    validation can be trivially bypassed by a hostname whose DNS answer
    changes between the two.
    """
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeConnectorURLError("url must be http or https")
    host = parsed.hostname
    if not host:
        raise UnsafeConnectorURLError("url must include a host")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeConnectorURLError(f"could not resolve host: {host}") from exc
    if not infos:
        raise UnsafeConnectorURLError(f"could not resolve host: {host}")

    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise UnsafeConnectorURLError(
                f"host {host} resolved to an unparseable address: {addr}"
            ) from exc
        if _is_disallowed_ip(ip):
            raise UnsafeConnectorURLError(
                f"host {host} resolves to a non-public address ({addr}); refusing"
            )
    return raw
