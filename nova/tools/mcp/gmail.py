"""Personal Gmail inbox via OAuth + Gmail JSON API (read-only).

Caller: nova/server/tool_service.py build_registry (replaces GmailStubTool).
Same pattern as calendar.py — REST until Workspace MCP tools/call unlocks.
"""
from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from nova.tools.base import NovaTool
from nova.tools.mcp.calendar import _oauth_access_or_error
from nova.tools.mcp.oauth import GoogleTokenProvider

_UNSET: Any = object()
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"


class CheckEmailTool(NovaTool):
    """Gmail inbox (OAuth + Gmail API): unread, latest, or summarize one message."""

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
        http_get=None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_get = http_get or httpx.get

    def execute(self, mode: str = "unread") -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        mode_key = (mode or "unread").strip().lower()
        if mode_key not in {"unread", "latest", "summarize"}:
            mode_key = "unread"

        headers = {"Authorization": f"Bearer {access}"}
        if mode_key == "summarize":
            return self._summarize(headers)

        q = "is:unread" if mode_key == "unread" else "in:inbox"
        max_n = 5 if mode_key == "unread" else 3
        try:
            listed = self._http_get(
                GMAIL_MESSAGES_URL,
                params={"q": q, "maxResults": max_n},
                headers=headers,
                timeout=self._timeout,
            )
            listed.raise_for_status()
            ids = [m["id"] for m in (listed.json().get("messages") or []) if m.get("id")]
        except Exception as exc:  # noqa: BLE001
            return _gmail_error(exc)

        messages: list[dict[str, str]] = []
        for mid in ids[:max_n]:
            try:
                resp = self._http_get(
                    GMAIL_MESSAGE_URL.format(id=mid),
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
                    headers=headers,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                messages.append(_normalize_message(resp.json()))
            except Exception:  # noqa: BLE001
                continue

        speak = _speak_messages(messages, mode_key)
        return {
            "status": "success",
            "mode": mode_key,
            "message_count": len(messages),
            "messages": messages,
            "speak": speak,
        }

    def _summarize(self, headers: dict[str, str]) -> dict[str, Any]:
        """Fetch body of first unread, else latest inbox message."""
        for q in ("is:unread", "in:inbox"):
            try:
                listed = self._http_get(
                    GMAIL_MESSAGES_URL,
                    params={"q": q, "maxResults": 1},
                    headers=headers,
                    timeout=self._timeout,
                )
                listed.raise_for_status()
                msgs = listed.json().get("messages") or []
                if not msgs or not msgs[0].get("id"):
                    continue
                mid = msgs[0]["id"]
                resp = self._http_get(
                    GMAIL_MESSAGE_URL.format(id=mid),
                    params={"format": "full"},
                    headers=headers,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                return _gmail_error(exc)

            meta = _normalize_message(payload)
            body = _extract_body_text(payload)
            body_short = _clip_for_speech(body, 600)
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
            }

        return {
            "status": "success",
            "mode": "summarize",
            "message": None,
            "speak": "There is no email to summarize.",
        }


def _gmail_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "gmail_api_error",
        "error": str(exc)[:300],
        "hint": (
            "If 403 insufficient scopes, Disconnect then Connect Google again "
            "to grant gmail.readonly."
        ),
        "speak": "Gmail is unavailable. Reconnect Google in Settings.",
    }


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = ((payload.get("payload") or {}).get("headers")) or []
    out: dict[str, str] = {}
    for h in headers:
        if isinstance(h, dict) and h.get("name"):
            out[str(h["name"]).lower()] = str(h.get("value") or "")
    return out


def _normalize_message(payload: dict[str, Any]) -> dict[str, str]:
    headers = _header_map(payload)
    return {
        "id": str(payload.get("id") or ""),
        "from": headers.get("from") or "unknown",
        "subject": headers.get("subject") or "(no subject)",
        "preview": str(payload.get("snippet") or "")[:160],
    }


def _extract_body_text(payload: dict[str, Any]) -> str:
    """Walk Gmail payload parts for text/plain (fallback text/html stripped)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime.startswith("text/"):
            try:
                raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                raw = ""
            if mime == "text/plain":
                plain.append(raw)
            elif mime == "text/html":
                html.append(raw)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    root = payload.get("payload")
    if isinstance(root, dict):
        walk(root)
    text = "\n".join(plain).strip()
    if not text and html:
        text = re.sub(r"<[^>]+>", " ", html[0])
        text = re.sub(r"\s+", " ", text).strip()
    return text


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
