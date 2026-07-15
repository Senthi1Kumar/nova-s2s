"""Shared helpers for Google Workspace remote MCP tool calls.

Callers: nova/tools/mcp/{calendar,gmail,drive}.py.
"""
from __future__ import annotations

import json
from typing import Any

from nova.tools.mcp.oauth import GoogleTokenProvider


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
