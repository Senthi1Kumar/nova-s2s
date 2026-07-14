"""HTTP JSON-RPC client for Google remote MCP servers.

Callers: nova/tools/mcp/calendar.py, scripts/google_mcp_auth.py.
No prior MCP HTTP client (Grep: calendarmcp / GoogleMcpClient absent).
Does not persist data files; uses GoogleTokenProvider for Authorization.
"""
from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

from nova.tools.mcp.oauth import GoogleTokenProvider

CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"
GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
PEOPLE_MCP_URL = "https://people.googleapis.com/mcp/v1"
DRIVE_MCP_URL = "https://drivemcp.googleapis.com/mcp/v1"


class GoogleMcpClient:
    """Minimal MCP client: initialize → tools/list|call over HTTPS JSON-RPC."""

    def __init__(
        self,
        base_url: str,
        tokens: GoogleTokenProvider | None = None,
        timeout: float = 20.0,
        require_auth: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens if tokens is not None else GoogleTokenProvider()
        self.timeout = timeout
        self.require_auth = require_auth
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.require_auth:
            token = self.tokens.get_access_token()
            if not token:
                raise RuntimeError("google_oauth_not_authenticated")
            headers["Authorization"] = f"Bearer {token}"
            headers["x-goog-user-project"] = self.tokens.project_id()
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.post(
            self.base_url,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            data = _parse_sse_json(resp.text)
        else:
            resp.raise_for_status()
            data = resp.json()
        if not resp.is_success and "error" not in (data or {}):
            resp.raise_for_status()
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            raise RuntimeError(f"mcp_error: {err}")
        return data if isinstance(data, dict) else {"result": data}

    def initialize(self) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "nova-s2s", "version": "0.1.0"},
            },
        }
        result = self._post(payload)
        try:
            httpx.post(
                self.base_url,
                headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            pass
        self._initialized = True
        return result

    def ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        self.ensure_initialized()
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "tools/list",
                "params": {},
            }
        )
        tools = (data.get("result") or {}).get("tools") or []
        return list(tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.ensure_initialized()
        data = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        return data.get("result")


def _parse_sse_json(text: str) -> dict[str, Any]:
    """Extract the last JSON payload from an SSE body."""
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                last = json.loads(raw)
            except json.JSONDecodeError:
                continue
    if last is None:
        raise RuntimeError("empty_sse_response")
    return last
