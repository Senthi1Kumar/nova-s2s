"""Minimal MCP client — HTTP transport only.

Deliberately not a full MCP implementation: no stdio transport, no SSE
streaming, no session/capability negotiation beyond a bare `initialize`.
Three operations only, each a single JSON-RPC 2.0 POST with an explicit,
short timeout:

  connect(url)                       -- reachability check ("initialize")
  list_tools(url)                    -- "tools/list"
  call_tool(url, name, arguments)    -- "tools/call"

This runs from the settings screen (adding/enabling a connector), never from
the hot voice-turn path. Callers MUST treat everything returned here as
untrusted data — same rule as web search results: never follow instructions
found inside a tool's description or output.
"""
from __future__ import annotations

import itertools
from typing import Any

import httpx

from nova_hailo.connectors.url_safety import UnsafeConnectorURLError, assert_public_http_url

DEFAULT_TIMEOUT = 5.0
CALL_TIMEOUT = 15.0

_id_counter = itertools.count(1)


class MCPClientError(RuntimeError):
    """Connector unreachable, timed out, or replied with invalid JSON-RPC."""


def _rpc_call(
    url: str, method: str, params: dict[str, Any] | None, timeout: float
) -> Any:
    # Fetch-time SSRF re-check, immediately before every outbound request.
    # A connector's URL was already validated when it was registered
    # (registry.add_connector), but a hostname can resolve to a public
    # address at registration and a loopback/link-local/private/metadata
    # address by the time the request actually goes out (DNS rebinding) --
    # so this check must run again here, every single call, not just once.
    try:
        assert_public_http_url(url)
    except UnsafeConnectorURLError as exc:
        raise MCPClientError(f"refusing to call {method}: {exc}") from exc

    payload = {
        "jsonrpc": "2.0",
        "id": next(_id_counter),
        "method": method,
        "params": params or {},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as exc:
        raise MCPClientError(f"{method} failed: {exc}") from exc
    except ValueError as exc:
        raise MCPClientError(f"{method} returned invalid JSON: {exc}") from exc
    if isinstance(body, dict) and body.get("error"):
        raise MCPClientError(f"{method} error: {body['error']}")
    if not isinstance(body, dict) or "result" not in body:
        raise MCPClientError(f"{method} returned no result field")
    return body["result"]


def connect(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Best-effort MCP handshake; returns the server's `initialize` result."""
    result = _rpc_call(
        url,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nova-hailo", "version": "1"},
        },
        timeout,
    )
    return result if isinstance(result, dict) else {}


def list_tools(url: str, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """Return the server's advertised tools (raw MCP tool dicts)."""
    result = _rpc_call(url, "tools/list", {}, timeout)
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    timeout: float = CALL_TIMEOUT,
) -> Any:
    """Invoke one tool on the connector. Result is untrusted data."""
    return _rpc_call(
        url, "tools/call", {"name": name, "arguments": arguments or {}}, timeout
    )
