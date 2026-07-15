"""Personal Gmail via OAuth + Workspace Gmail MCP.

Caller: nova/server/tool_service.py build_registry.
Uses remote MCP tools/call (search_threads / get_message).
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from nova.tools.base import NovaTool
from nova.tools.mcp.client import GMAIL_MCP_URL, GoogleMcpClient
from nova.tools.mcp.oauth import GoogleTokenProvider
from nova.tools.mcp.runtime import oauth_ready_or_error, unpack_mcp_result

_UNSET: Any = object()


class _McpCaller(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


class CheckEmailTool(NovaTool):
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
        timeout: float = 20.0,
        mcp: _McpCaller | None = None,
        http_get=None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._mcp = mcp
        _ = http_get

    def _client(self) -> _McpCaller:
        if self._mcp is not None:
            return self._mcp
        return GoogleMcpClient(GMAIL_MCP_URL, tokens=self._tokens, timeout=self._timeout)

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
            raw = self._client().call_tool(
                "search_threads", {"query": q, "pageSize": max_n}
            )
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
                raw = self._client().call_tool(
                    "search_threads", {"query": q, "pageSize": 1}
                )
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
                    raw_msg = self._client().call_tool(
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
