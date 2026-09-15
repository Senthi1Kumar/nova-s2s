"""Personal Gmail via OAuth + Workspace Gmail MCP, on the shared transport.

Caller: nova_hailo.edge_harness.tool_broker.ToolBroker._mcp_gmail.
Ported from nova/tools/mcp/gmail.py: every call used to rebuild an MCP client
and redo the `initialize` handshake, which was the single largest cost in
this tool's latency. `_call` now goes through
nova_hailo.connectors.mcp_transport, which caches one handshake per URL and
reuses it for every later call. The parsing and speech helpers below are
copied unchanged -- they are voice-shaped already and are not what was slow.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from nova_hailo.connectors import mcp_transport
from nova_hailo.connectors.workspace.runtime import (
    oauth_ready_or_error,
    unpack_mcp_result,
    unwrap_call_tool_result,
)
from nova_hailo.google_oauth import GoogleTokenProvider

GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"

_UNSET: Any = object()


class _McpCaller(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


class CheckEmailTool:
    """Gmail inbox (OAuth + Gmail MCP): unread, latest, or summarize one message."""

    name = "check_email"
    description = (
        "Check the user's Gmail. mode=unread (default), mode=latest, or mode=summarize "
        "to fetch the body of the top unread/latest message for a short spoken summary. "
        "Speak the returned speak field verbatim — never say you lack email access."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["unread", "latest", "summarize"],
                "description": "unread, latest, or summarize (body of top message).",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 8.0,
        mcp: _McpCaller | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        # Test seam: an injected caller bypasses mcp_transport (and its
        # process-wide session cache) entirely -- see _call() below.
        self._mcp = mcp

    def _auth_headers(self) -> dict[str, str]:
        # Built fresh per request (never cached) so a token refreshed mid-turn
        # is never masked by a header captured before it rotated.
        token = self._tokens.get_access_token() or ""
        return {"Authorization": f"Bearer {token}"}

    def _call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._mcp is not None:
            return self._mcp.call_tool(name, arguments)
        envelope = mcp_transport.call_tool(
            GMAIL_MCP_URL,
            name,
            arguments,
            headers_fn=self._auth_headers,
            timeout=self._timeout,
        )
        return unwrap_call_tool_result(envelope)

    def execute(self, mode: str = "unread") -> dict[str, Any]:
        err = oauth_ready_or_error(self._tokens)
        if err is not None:
            return {
                **err,
                "speak": "Gmail is unavailable. Reconnect Google in Settings.",
            }

        mode_key = (mode or "unread").strip().lower()
        if mode_key not in {"unread", "latest", "summarize"}:
            mode_key = "unread"

        if mode_key == "summarize":
            return self._summarize()

        q = "is:unread" if mode_key == "unread" else "in:inbox"
        max_n = 5 if mode_key == "unread" else 3
        try:
            raw = self._call("search_threads", {"query": q, "pageSize": max_n})
            payload = unpack_mcp_result(raw)
        except Exception as exc:  # noqa: BLE001
            return _gmail_error(exc)

        messages = _threads_to_messages(payload)[:max_n]
        speak = _speak_messages(messages, mode_key)
        return {
            "status": "success",
            "mode": mode_key,
            "message_count": len(messages),
            "messages": messages,
            "speak": speak,
            "source": "google_gmail_mcp",
        }

    def _summarize(self) -> dict[str, Any]:
        for q in ("is:unread", "in:inbox"):
            try:
                raw = self._call("search_threads", {"query": q, "pageSize": 1})
                payload = unpack_mcp_result(raw)
            except Exception as exc:  # noqa: BLE001
                return _gmail_error(exc)

            messages = _threads_to_messages(payload)
            if not messages:
                continue
            meta = messages[0]
            mid = meta.get("id") or ""
            body_short = ""
            if mid:
                try:
                    raw_msg = self._call(
                        "get_message",
                        {"messageId": mid, "messageFormat": "FULL_CONTENT"},
                    )
                    msg_payload = unpack_mcp_result(raw_msg)
                    body_short = _clip_for_speech(_extract_mcp_body(msg_payload), 600)
                    meta = {**meta, **_normalize_mcp_message(msg_payload)}
                except Exception:  # noqa: BLE001
                    body_short = _clip_for_speech(meta.get("preview") or "", 600)

            sender = (meta.get("from") or "unknown").split("<")[0].strip().rstrip(",")
            subject = meta.get("subject") or "no subject"
            if body_short:
                speak = (
                    f"Email from {sender}, subject {subject}. "
                    f"Summary of the content: {body_short}"
                )
            else:
                preview = meta.get("preview") or "No body text available."
                speak = f"Email from {sender}, subject {subject}. Preview: {preview}"
            return {
                "status": "success",
                "mode": "summarize",
                "message": {**meta, "body": body_short},
                "speak": speak,
                "source": "google_gmail_mcp",
            }

        return {
            "status": "success",
            "mode": "summarize",
            "message": None,
            "speak": "There is no email to summarize.",
            "source": "google_gmail_mcp",
        }


def _gmail_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "gmail_mcp_error",
        "error": str(exc)[:300],
        "hint": (
            "If 403 insufficient scopes, Disconnect then Connect Google again "
            "to grant gmail.readonly."
        ),
        "speak": "Gmail is unavailable. Reconnect Google in Settings.",
    }


def _threads_to_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    threads = payload.get("threads") or []
    out: list[dict[str, str]] = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        msgs = thread.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            out.append(_normalize_mcp_message(msgs[0]))
        else:
            out.append(
                {
                    "id": str(thread.get("id") or ""),
                    "from": "unknown",
                    "subject": "(no subject)",
                    "preview": "",
                }
            )
    return out


def _normalize_mcp_message(payload: dict[str, Any]) -> dict[str, str]:
    sender = (
        payload.get("sender")
        or payload.get("from")
        or payload.get("from_")
        or "unknown"
    )
    subject = payload.get("subject") or "(no subject)"
    preview = str(payload.get("snippet") or payload.get("preview") or "")[:160]
    mid = payload.get("id") or payload.get("messageId") or ""
    return {
        "id": str(mid),
        "from": str(sender),
        "subject": str(subject),
        "preview": preview,
    }


def _extract_mcp_body(payload: dict[str, Any]) -> str:
    for key in ("plaintextBody", "plaintext_body", "body", "text"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    msg = payload.get("message")
    if isinstance(msg, dict):
        return _extract_mcp_body(msg)
    return str(payload.get("snippet") or "")


def _clip_for_speech(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _speak_messages(messages: list[dict[str, str]], mode: str) -> str:
    if not messages:
        if mode == "latest":
            return "Your inbox has no recent emails."
        return "You have no unread emails."
    n = len(messages)
    parts = []
    for m in messages[:4]:
        sender = (m.get("from") or "unknown").split("<")[0].strip().rstrip(",")
        parts.append(f"{sender}: {m.get('subject') or 'no subject'}")
    more = f" And {n - 4} more." if n > 4 else ""
    if mode == "latest":
        return f"Your latest email{'s' if n != 1 else ''}: " + "; ".join(parts) + "." + more
    return f"You have {n} unread emails. " + "; ".join(parts) + "." + more
