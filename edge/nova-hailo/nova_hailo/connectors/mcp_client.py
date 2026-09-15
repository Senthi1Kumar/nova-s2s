"""Minimal MCP client -- generic third-party connector transport.

Thin wrapper over `mcp_transport` (session caching and connection pooling)
for user-registered, arbitrary MCP servers added from the settings screen.
Three operations, each one JSON-RPC 2.0 round trip via the shared transport:

  connect(url)                       -- reachability check ("initialize")
  list_tools(url)                    -- "tools/list"
  call_tool(url, name, arguments)    -- "tools/call"

`mcp_transport` itself never does the SSRF re-check below -- it is built
against known, already-trusted endpoints (Google's). This module talks to
whatever URL a user pastes into settings, so the re-check and the
MCPClientError mapping both live here, not in the shared transport.

This runs from the settings screen (adding/enabling a connector), never from
the hot voice-turn path. Callers MUST treat everything returned here as
untrusted data -- same rule as web search results: never follow instructions
found inside a tool's description or output.
"""
from __future__ import annotations

from typing import Any

import httpx

from nova_hailo.connectors import mcp_transport
from nova_hailo.connectors.url_safety import UnsafeConnectorURLError, assert_public_http_url

DEFAULT_TIMEOUT = 5.0
CALL_TIMEOUT = 15.0


class MCPClientError(RuntimeError):
    """Connector unreachable, timed out, or replied with invalid JSON-RPC."""


def _guarded_url(url: str, method: str) -> str:
    # Fetch-time SSRF re-check, immediately before every outbound request.
    # A connector's URL was already validated when it was registered
    # (registry.add_connector), but a hostname can resolve to a public
    # address at registration and a loopback/link-local/private/metadata
    # address by the time the request actually goes out (DNS rebinding) --
    # so this check must run again here, every single call, not just once.
    try:
        return assert_public_http_url(url)
    except UnsafeConnectorURLError as exc:
        raise MCPClientError(f"refusing to call {method}: {exc}") from exc


def _unwrap_result(envelope: Any, method: str) -> Any:
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise MCPClientError(f"{method} returned no result field")
    return envelope["result"]


def _call(url: str, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
    safe_url = _guarded_url(url, method)
    try:
        envelope = mcp_transport.call(safe_url, method, params, timeout=timeout)
    except mcp_transport.McpError as exc:
        raise MCPClientError(f"{method} failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise MCPClientError(f"{method} failed: {exc}") from exc
    except RuntimeError as exc:
        # E.g. httpx's "Cannot send a request, as the client has been
        # closed." when one thread's expiry-retry closes a client another
        # thread is mid-request on -- neither McpError nor HTTPError, so it
        # must be mapped here too or it escapes the transport unwrapped.
        raise MCPClientError(f"{method} failed: {exc}") from exc
    return _unwrap_result(envelope, method)


def connect(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Best-effort MCP handshake; returns the server's `initialize` result."""
    result = _call(
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
    result = _call(url, "tools/list", {}, timeout)
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
    safe_url = _guarded_url(url, "tools/call")
    try:
        envelope = mcp_transport.call_tool(safe_url, name, arguments, timeout=timeout)
    except mcp_transport.McpError as exc:
        raise MCPClientError(f"tools/call failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise MCPClientError(f"tools/call failed: {exc}") from exc
    except RuntimeError as exc:
        # See the matching comment in _call() above.
        raise MCPClientError(f"tools/call failed: {exc}") from exc
    return _unwrap_result(envelope, "tools/call")
