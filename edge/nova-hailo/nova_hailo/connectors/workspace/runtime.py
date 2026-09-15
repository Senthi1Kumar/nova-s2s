"""Shared helpers for Google Workspace remote MCP tool calls.

Callers: nova_hailo/connectors/workspace/{gmail,calendar,drive}.py, all on
the same helpers.

`oauth_ready_or_error` and `unpack_mcp_result` are ported from
nova/tools/mcp/runtime.py unchanged apart from the OAuth provider import --
nova-hailo has its own `GoogleTokenProvider` reading the same token file, and
two providers mutating one token file would race each other on refresh.

`unwrap_call_tool_result` is new here: the old stack's `GoogleMcpClient.call_tool`
returned just the JSON-RPC `result` object, which is the shape
`unpack_mcp_result` expects. The shared `mcp_transport.call_tool` instead
returns the whole envelope (`{"jsonrpc": ..., "id": ..., "result": {...}}`),
so every workspace tool built on it needs this one extra unwrap step first.

A reply with no `result` field at all, one whose `result` is JSON `null`, or
that isn't even a JSON object, is not a malformed-but-readable MCP response
-- it is not an MCP response, and must not be treated as "no results".
Silently returning `{}` (or `None`) there let a transport-level failure
masquerade as a genuinely empty inbox/calendar/drive listing (a false "you
have no unread emails" spoken as fact). All three of those cases raise
`McpError` instead, so the caller's existing exception handling turns them
into an honest "unavailable", not an invented empty result. A `result` field
that *is* present and non-None but not itself a dict (some other JSON
scalar/list) is a different, milder case -- the server replied, just not in
the expected shape -- so that value is passed through unchanged for
`unpack_mcp_result`'s own `{"value": result}` fallback to handle, exactly as
it already does for any other non-dict result.
"""
from __future__ import annotations

import json
from typing import Any

from nova_hailo.connectors import mcp_transport
from nova_hailo.google_oauth import GoogleTokenProvider

# Sentinel distinct from any real JSON-RPC `result` value (including `None`,
# which JSON-RPC allows as a legitimate result but which this module treats
# as an invalid envelope -- see unwrap_call_tool_result below).
_MISSING: Any = object()


def oauth_ready_or_error(tokens: GoogleTokenProvider | None) -> dict[str, Any] | None:
    """Return an unavailable payload, or None when OAuth is ready for MCP calls."""
    if tokens is None or not tokens.configured():
        return {
            "status": "unavailable",
            "reason": "google_oauth_not_configured",
            "hint": "Set GOOGLE_OAUTH_CLIENT_ID/SECRET in .env, then connect Google in Settings.",
        }
    if not tokens.authenticated():
        return {
            "status": "unavailable",
            "reason": "google_oauth_not_authenticated",
            "hint": "Open Settings → Connect with Google (work Chrome profile).",
        }
    if not tokens.get_access_token():
        return {
            "status": "unavailable",
            "reason": "google_oauth_token_refresh_failed",
            "hint": "Reconnect Google in Settings.",
        }
    return None


def unpack_mcp_result(result: Any) -> dict[str, Any]:
    """Normalize MCP tools/call result into a dict (prefer structuredContent)."""
    if not isinstance(result, dict):
        return {"value": result}
    if result.get("isError"):
        text = _content_text(result)
        raise RuntimeError(text or "mcp_tool_error")
    structured = result.get("structuredContent") or result.get("structured_content")
    if isinstance(structured, dict) and structured:
        return structured
    text = _content_text(result)
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"text": text}
    return result


def _content_text(result: dict[str, Any]) -> str:
    chunks: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            chunks.append(str(block.get("text") or ""))
    return "\n".join(chunks).strip()


def unwrap_call_tool_result(envelope: Any) -> Any:
    """Pull the inner JSON-RPC `result` out of a `call_tool` reply.

    See the module docstring for why a missing, `null`, or otherwise absent
    `result` raises instead of degrading to an empty/None value, while a
    present-and-non-None-but-non-dict `result` is passed through unchanged
    rather than dropped.
    """
    result = envelope.get("result", _MISSING) if isinstance(envelope, dict) else _MISSING
    if result is _MISSING or result is None:
        raise mcp_transport.McpError(
            f"not a valid MCP call_tool envelope (no result field): {envelope!r}"[:300]
        )
    return result
