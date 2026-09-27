"""The realtime socket URL a caller should dial, as served by GET /config.

Callers: nova_hailo/web/app.py config().

The scheme follows the request (https page -> wss socket), so a phone that
loaded the orb over the TLS listener is not sent to plain ws://, which the
browser blocks as mixed content. The token is echoed only when the caller
itself presented it in the query string -- /config never hands the configured
secret to anyone who did not already have it.
"""
from __future__ import annotations

from urllib.parse import urlencode


def public_ws_url(
    *,
    scheme: str,
    host_header: str,
    default_host: str,
    default_port: int,
    query_token: str | None,
) -> str:
    host, port = default_host, default_port
    if host_header:
        if ":" in host_header:
            h, _, p = host_header.rpartition(":")
            host = h
            try:
                port = int(p)
            except ValueError:
                pass
        else:
            host = host_header
    ws_scheme = "wss" if scheme == "https" else "ws"
    url = f"{ws_scheme}://{host}:{port}/v1/realtime"
    if query_token:
        url += "?" + urlencode({"token": query_token})
    return url
