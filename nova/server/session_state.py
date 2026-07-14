"""Per-session scratch for tool-agent follow-ups (email / Drive refs).

Callers: ``nova.server.tool_agent`` when ``session_id`` is set on ``/tools/agent``.
Keeps the last check_email / list_drive_files payloads so follow-ups like
"summarize that email", "reply to that email", and "the third file" can resolve
without re-asking the 230M router for context it never saw.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

_ORDINAL_WORDS: dict[str, int] = {
    "first": 0,
    "1st": 0,
    "second": 1,
    "2nd": 1,
    "third": 2,
    "3rd": 2,
    "fourth": 3,
    "4th": 3,
    "fifth": 4,
    "5th": 4,
}
_ORDINAL_LABELS = ("first", "second", "third", "fourth", "fifth")


@dataclass
class SessionScratch:
    last_emails: list[dict[str, Any]] = field(default_factory=list)
    last_email_detail: dict[str, Any] | None = None
    last_drive_files: list[dict[str, Any]] = field(default_factory=list)
    draft_reply: dict[str, str] | None = None


_lock = threading.Lock()
_sessions: dict[str, SessionScratch] = {}


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def get_session(session_id: str | None) -> SessionScratch:
    sid = (session_id or "").strip() or "_default"
    with _lock:
        scratch = _sessions.get(sid)
        if scratch is None:
            scratch = SessionScratch()
            _sessions[sid] = scratch
        return scratch


def remember_tool_result(
    session_id: str | None, name: str, result: dict[str, Any]
) -> None:
    """Update scratch from a successful tool result (best-effort)."""
    if not isinstance(result, dict) or result.get("status") != "success":
        return
    scratch = get_session(session_id)
    if name == "check_email":
        msgs = result.get("messages")
        if isinstance(msgs, list) and msgs:
            scratch.last_emails = [m for m in msgs if isinstance(m, dict)]
        detail = result.get("message")
        if isinstance(detail, dict) and detail:
            scratch.last_email_detail = detail
            scratch.last_emails = [detail] + [
                m for m in scratch.last_emails if m.get("id") != detail.get("id")
            ]
    elif name == "list_drive_files":
        files = result.get("files")
        if isinstance(files, list):
            scratch.last_drive_files = [f for f in files if isinstance(f, dict)]


def email_mode_from_query(query: str) -> str:
    """Match live s2s ``_args_for_forced_tool`` check_email heuristics."""
    q = (query or "").lower()
    if any(
        w in q
        for w in (
            "summar",
            "summary",
            "summaris",
            "what does it say",
            "what's it about",
            "read it",
            "body",
        )
    ):
        return "summarize"
    if any(w in q for w in ("latest", "last", "recent", "newest")):
        return "latest"
    if any(w in q for w in ("unread", "new mail", "new email")):
        return "unread"
    # Bare "check my emails" → latest so an empty unread inbox is not a dead end.
    if re.search(r"\bemails?\b", q):
        return "latest"
    return "unread"


def _reply_address(email: dict[str, Any]) -> str:
    from_hdr = str(email.get("from") or "")
    m = re.search(r"<([^>]+)>", from_hdr)
    if m:
        return m.group(1).strip()
    if "@" in from_hdr:
        return from_hdr.strip()
    return ""


def ground_send_email_args(
    query: str, args: dict[str, Any], scratch: SessionScratch
) -> dict[str, Any]:
    """Fill to/subject/body from last email when the user refers to 'that email'."""
    out = dict(args)
    email = scratch.last_email_detail or (
        scratch.last_emails[0] if scratch.last_emails else None
    )
    q = (query or "").lower()
    refers = "that email" in q or "the email" in q or "this email" in q
    wants_draft = any(w in q for w in ("reply", "draft", "respond", "write back"))
    if not email or not (refers or wants_draft or scratch.draft_reply):
        return out

    addr = _reply_address(email)
    subject = str(email.get("subject") or "").strip()
    if addr and not (out.get("to") or "").strip():
        out["to"] = addr
    if subject and not (out.get("subject") or "").strip():
        out["subject"] = (
            subject if subject.lower().startswith("re:") else f"Re: {subject}"
        )
    if not (out.get("body") or "").strip():
        saying = re.search(r"saying\s+(.+)$", query or "", re.I)
        if saying:
            out["body"] = saying.group(1).strip().rstrip(".")
        elif "thanks" in q:
            out["body"] = "Thanks."
        elif scratch.draft_reply and scratch.draft_reply.get("body"):
            out["body"] = scratch.draft_reply["body"]

    # Persist grounded draft for a later confirm turn.
    if out.get("to") and out.get("subject") and out.get("body"):
        scratch.draft_reply = {
            "to": str(out["to"]),
            "subject": str(out["subject"]),
            "body": str(out["body"]),
        }
    return out


def resolve_drive_ordinal(
    query: str, files: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve 'the third file' against the last Drive listing."""
    q = (query or "").lower()
    idx: int | None = None
    for word, i in _ORDINAL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", q):
            idx = i
            break
    if idx is None:
        m = re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:file|document|doc)\b", q)
        if m:
            idx = int(m.group(1)) - 1
    if idx is None:
        return None
    if not files:
        return {
            "status": "error",
            "reason": "no_drive_context",
            "speak": "I don't have a recent Drive file list to pick from.",
        }
    if idx < 0 or idx >= len(files):
        return {
            "status": "error",
            "reason": "index_out_of_range",
            "speak": f"There is no file number {idx + 1} in the recent list.",
        }
    chosen = files[idx]
    name = str(chosen.get("name") or "untitled")
    label = _ORDINAL_LABELS[idx] if idx < len(_ORDINAL_LABELS) else str(idx + 1)
    return {
        "status": "success",
        "file": chosen,
        "speak": f"The {label} file is {name}.",
    }


def unsupported_capability(query: str) -> dict[str, Any] | None:
    """Known missing capabilities — never fabricate a success payload."""
    tokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    delete_words = {"delete", "remove", "trash", "erase"}
    drive_words = {"drive", "file", "files", "document", "documents", "doc", "folder"}
    calendar_words = {"calendar", "event", "meeting", "appointment", "reminder"}
    if tokens & delete_words and tokens & drive_words and not (tokens & calendar_words):
        return {
            "status": "unsupported",
            "reason": "capability_missing",
            "capability": "delete_drive_file",
            "speak": "I can't delete Drive files yet — that isn't supported.",
        }
    return None
