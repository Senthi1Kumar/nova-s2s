"""OEM allowlist tool gateway: websearch FastMCP + optional Workspace MCP.

Callers: nova_hailo.pipeline (allowlist router).
v0.0.1 is conversation-only (profile: conversation, enabled: []). web_search
and deep_research (Brave→Serper; Tavily async) are built and tested here but
gated off; they return in v0.0.2.
API: OemToolGateway.route_and_execute(query)->{ok,name,speak,status,result}; never invents results.
"""
from __future__ import annotations

import os
import re
from typing import Any

from nova_hailo.tools.search_mcp import deep_research as mcp_deep_research
from nova_hailo.tools.search_mcp import research_status as mcp_research_status
from nova_hailo.tools.search_mcp import web_search as mcp_web_search

OEM_READONLY = ("check_calendar", "check_email", "list_drive_files", "web_search")
WEBSEARCH_READONLY = ("web_search", "deep_research")

_CAL_RE = re.compile(
    r"\b(calendar|schedule|meetings?|what's on|whats on|agenda|appointments?)\b", re.I
)
_MAIL_RE = re.compile(r"\b(email|inbox|gmail|mail from|unread)\b", re.I)
_DRIVE_RE = re.compile(r"\b(drive|files?|documents?|docs?|folder)\b", re.I)
# Explicit search / lookup intents only — do not match casual "what is your name?".
# Bare "news" / "current news today" must match — otherwise the chat LLM + soul.md
# ("no internet") refuses and identity-drifts (measured 2026-08-04 live session).
_WEB_RE = re.compile(
    r"\b("
    r"search(\s+the\s+web|\s+online|\s+for)?|"
    r"look\s*up|google|"
    r"(current|latest|today'?s?|breaking)\s+news|"
    r"news(\s+(about|on|in|from|today|latest))?|"
    r"headlines?|"
    r"who\s+won|weather|forecast|stock\s+prices?"
    r")\b",
    re.I,
)
_RESEARCH_RE = re.compile(
    r"\b(research|dig\s+into|deep\s+dive|look\s+into|investigate)\b",
    re.I,
)
_CHAT_BLOCK_WEB = re.compile(
    r"\b(your\s+name|who\s+are\s+you|what\s+can\s+you\s+do|introduce\s+yourself)\b",
    re.I,
)
# Identity/capability turns answer deterministically: a 1.5B model's own identity
# reply is what seeds the in-context repetition lock (measured 2026-07-29), and the
# canned path also drops these turns from ~2.1s to ~0.2s TTFA.
_IDENTITY_RE = re.compile(
    r"\b(your\s+name|who\s+are\s+you|who\s+is\s+nova|what\s+are\s+you|"
    r"introduce\s+yourself)\b",
    re.I,
)
_CAPABILITY_RE = re.compile(
    r"\b(what\s+can\s+you\s+do|what\s+do\s+you\s+do|how\s+can\s+you\s+help|"
    r"what\s+else\s+can\s+you\s+do)\b",
    re.I,
)
CAPABILITY_SPEAK = (
    "I can chat, and I can look things up on the web when you ask. "
    "Calendar and email are still coming."
)
IDENTITY_SPEAK = "I'm Nova, your car's built-in assistant. Happy to keep you company."

# Smalltalk answered deterministically: highest-frequency demo turns, and every
# one removed from the LLM path drops ~3.5s to <0.8s and cannot repetition-lock.
_SMALLTALK: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Whole-utterance only. A prefix match swallowed real questions:
        # "Hey there can we chat about the latest BMW?" answered "Hello."
        re.compile(
            r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening))"
            r"[\s,!.]*(there|nova|buddy)?[\s,!.]*$",
            re.I,
        ),
        "Hello. How can I help?",
    ),
    (
        re.compile(r"\b(thanks|thank\s+you|cheers|appreciate\s+it)\b", re.I),
        "Anytime.",
    ),
    (
        re.compile(r"\b(goodbye|bye|see\s+you|good\s*night|that'?s\s+all)\b", re.I),
        "Goodbye. Talk soon.",
    ),
    (
        # Whole-utterance (with an optional short prefix/suffix), not a
        # substring match. "stop"/"cancel" are common English words, and a
        # bare \b match swallowed real questions: garbled ASR turned "stock
        # price of Tesla" into "stop price of Tesla" and this fired
        # "Okay, stopping." instead of routing to search (measured 2026-08-04).
        re.compile(
            r"^\s*(hey\s+nova|hey|nova|ok(ay)?)?[\s,!.]*"
            r"(stop|cancel|never\s*mind|forget\s+it)"
            r"(\s+(that|it|please|now))?[\s,!.]*$",
            re.I,
        ),
        "Okay, stopping.",
    ),
)


# Capability asks with no implementation in this profile. A 1.5B cannot be
# prompted into reliably declining these — measured 2026-07-30 it invented a
# clock time, a fake email result and a false reminder promise. Each entry is
# (pattern, required_tool, honest_reply); the decline only fires when that tool
# is NOT enabled, so it never shadows a real tool in oem_readonly.
_UNAVAILABLE_CAPS: tuple[tuple[object, str, str], ...] = (
    (re.compile(r"\b(what|whats|what's)\s+(the\s+)?(time|date|day)\b", re.I),
     "get_datetime", "I can't read the clock yet, sorry."),
    (re.compile(r"\b(weather|forecast|raining|temperature)\b", re.I),
     "web_search", "I can't check the weather yet."),
    (re.compile(r"\b(traffic|route\s+to|navigate)\b", re.I),
     "navigation", "I can't check traffic or routes yet."),
    (re.compile(r"\b(email|inbox|gmail)\b", re.I),
     "check_email", "I can't get into your email yet."),
    (re.compile(r"\b(calendar|schedule|meetings?|agenda|appointments?)\b", re.I),
     "check_calendar", "I can't see your calendar yet."),
    (re.compile(r"\b(remind\s+me|reminder)\b", re.I),
     "create_reminder", "I can't set reminders yet, but I'm listening."),
    (re.compile(r"\b(news|headlines|stock|share\s+price)\b", re.I),
     "web_search", "I can't look that up yet."),
    (re.compile(r"\b(search\s+the\s+web|google\s+it|look\s+it\s+up)\b", re.I),
     "web_search", "I can't search the web yet."),
    (re.compile(r"\b(research|dig\s+into|deep\s+dive|look\s+into|investigate)\b", re.I),
     "deep_research", "I can't do deep research yet."),
)


def _unavailable_cap_speak(query: str, enabled: set) -> str | None:
    """Honest decline for a capability this profile does not implement."""
    for pattern, tool, speak in _UNAVAILABLE_CAPS:
        if tool not in enabled and pattern.search(query):
            return speak
    return None


def _smalltalk_speak(query: str) -> str | None:
    for pattern, speak in _SMALLTALK:
        if pattern.search(query):
            return speak
    return None


def _unavailable(name: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "name": name,
        "status": "unavailable",
        "reason": reason,
        "speak": "I can't reach that service right now.",
        "result": None,
    }


def _ok(name: str, speak: str, result: Any = None) -> dict[str, Any]:
    return {
        "ok": True,
        "name": name,
        "status": "success",
        "speak": speak,
        "result": result,
    }


class OemToolGateway:
    """Deterministic allowlist router + fail-closed adapters."""

    def __init__(
        self,
        enabled: list[str] | None = None,
        *,
        timeout_sec: float = 1.5,
        write_enabled: bool = False,
        serper_fallback: bool = True,
    ):
        # `enabled=[]` must mean "no tools", not "all tools" — an empty list is
        # falsy, so `or OEM_READONLY` silently enabled everything in the
        # conversation profile (measured 2026-07-30).
        self.enabled = set(OEM_READONLY if enabled is None else enabled)
        self.timeout_sec = float(timeout_sec)
        self.write_enabled = bool(write_enabled)
        self.serper_fallback = bool(serper_fallback)

    def route(self, query: str) -> str | None:
        q = (query or "").strip()
        if not q:
            return None
        # Identity first: never let the LLM author its own identity line.
        if _IDENTITY_RE.search(q):
            return "identity"
        if _CAPABILITY_RE.search(q):
            return "capability"
        if _smalltalk_speak(q):
            return "smalltalk"
        if _unavailable_cap_speak(q, self.enabled):
            return "capability_unavailable"
        if "check_calendar" in self.enabled and _CAL_RE.search(q):
            return "check_calendar"
        if "check_email" in self.enabled and _MAIL_RE.search(q):
            return "check_email"
        if "list_drive_files" in self.enabled and _DRIVE_RE.search(q):
            return "list_drive_files"
        # Research before quick search so "research X" never collapses to web_search.
        if (
            "deep_research" in self.enabled
            and _RESEARCH_RE.search(q)
            and not _CHAT_BLOCK_WEB.search(q)
        ):
            return "deep_research"
        if "web_search" in self.enabled and _WEB_RE.search(q) and not _CHAT_BLOCK_WEB.search(q):
            return "web_search"
        return None

    def route_and_execute(self, query: str) -> dict[str, Any] | None:
        name = self.route(query)
        if not name:
            return None
        return self.execute(name, query=query)

    def execute(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if name == "identity":
            return _ok("identity", IDENTITY_SPEAK)
        if name == "capability":
            return _ok("capability", CAPABILITY_SPEAK)
        if name == "smalltalk":
            speak = _smalltalk_speak(str(kwargs.get("query") or ""))
            if speak:
                return _ok("smalltalk", speak)
            return _unavailable("smalltalk", "no_smalltalk_match")
        if name == "capability_unavailable":
            speak = _unavailable_cap_speak(str(kwargs.get("query") or ""), self.enabled)
            if speak:
                return _ok("capability_unavailable", speak)
            return _unavailable("capability_unavailable", "no_capability_match")
        if name not in self.enabled:
            return _unavailable(name, "tool_not_in_oem_allowlist")
        if name in {"create_calendar_event", "delete_calendar_event", "send_email"} and not self.write_enabled:
            return _unavailable(name, "writes_disabled_for_oem_demo")
        if name == "web_search":
            return self._web_search(str(kwargs.get("query") or ""))
        if name == "deep_research":
            return mcp_deep_research(
                str(kwargs.get("query") or ""),
                timeout_sec=max(self.timeout_sec, 12.0),
            )
        if name == "research_status":
            return mcp_research_status(str(kwargs.get("job_id") or ""))
        if name == "check_calendar":
            return self._mcp_calendar(str(kwargs.get("query") or ""))
        if name == "check_email":
            return self._mcp_gmail(str(kwargs.get("query") or ""))
        if name == "list_drive_files":
            return self._mcp_drive(str(kwargs.get("query") or ""))
        return _unavailable(name, "unknown_tool")

    def poll_research(self, job_id: str) -> dict[str, Any]:
        return mcp_research_status(job_id)

    def _web_search(self, query: str) -> dict[str, Any]:
        return mcp_web_search(
            query,
            timeout_sec=self.timeout_sec,
            serper_fallback=self.serper_fallback,
        )

    def _ensure_workspace_auth(self) -> dict[str, Any] | None:
        """Require one-time UI/CLI OAuth tokens stored by nova_hailo.google_oauth."""
        try:
            from nova_hailo.google_oauth import GoogleTokenProvider
        except Exception as exc:  # noqa: BLE001
            return _unavailable("workspace", f"oauth_import:{exc}")
        provider = GoogleTokenProvider()
        if not provider.configured():
            return _unavailable(
                "workspace",
                "oauth_not_configured",
            )
        if not provider.authenticated():
            return _unavailable(
                "workspace",
                "oauth_not_connected",
            )
        # Point any optional MCP adapters at the same token file
        os.environ.setdefault("GOOGLE_OAUTH_TOKEN_PATH", str(provider.store.path))
        return None

    def _mcp_calendar(self, query: str) -> dict[str, Any]:
        blocked = self._ensure_workspace_auth()
        if blocked is not None:
            blocked["name"] = "check_calendar"
            blocked["speak"] = "Google Workspace is not connected. Open Settings and tap Connect with Google."
            return blocked
        try:
            from nova.tools.mcp.calendar import CheckCalendarTool
        except Exception as exc:  # noqa: BLE001
            return _unavailable("check_calendar", f"mcp_import:{exc}")
        try:
            tool = CheckCalendarTool()
            # CheckCalendarTool.execute(day=..., on_date=...) — it does not take
            # a free-text query, so map the spoken phrasing onto its day window.
            q = (query or "").lower()
            if "tomorrow" in q:
                day = "tomorrow"
            elif "today" in q or "tonight" in q:
                day = "today"
            else:
                day = "week"
            out = tool.execute(day=day)
            return _normalize_mcp("check_calendar", out)
        except Exception as exc:  # noqa: BLE001
            return _unavailable("check_calendar", f"mcp_call:{exc}")

    def _mcp_gmail(self, query: str) -> dict[str, Any]:
        blocked = self._ensure_workspace_auth()
        if blocked is not None:
            blocked["name"] = "check_email"
            blocked["speak"] = "Google Workspace is not connected. Open Settings and tap Connect with Google."
            return blocked
        try:
            from nova.tools.mcp.gmail import CheckEmailTool
        except Exception as exc:  # noqa: BLE001
            return _unavailable("check_email", f"mcp_import:{exc}")
        try:
            tool = CheckEmailTool()
            # CheckEmailTool.execute(mode="unread"|...) — no free-text query.
            out = tool.execute(mode="unread")
            return _normalize_mcp("check_email", out)
        except Exception as exc:  # noqa: BLE001
            return _unavailable("check_email", f"mcp_call:{exc}")

    def _mcp_drive(self, query: str) -> dict[str, Any]:
        blocked = self._ensure_workspace_auth()
        if blocked is not None:
            blocked["name"] = "list_drive_files"
            blocked["speak"] = "Google Workspace is not connected. Open Settings and tap Connect with Google."
            return blocked
        try:
            from nova.tools.mcp.drive import ListDriveFilesTool
        except Exception as exc:  # noqa: BLE001
            return _unavailable("list_drive_files", f"mcp_import:{exc}")
        try:
            tool = ListDriveFilesTool()
            out = tool.execute(query=query or "")
            return _normalize_mcp("list_drive_files", out)
        except Exception as exc:  # noqa: BLE001
            return _unavailable("list_drive_files", f"mcp_call:{exc}")


def _normalize_mcp(name: str, out: Any) -> dict[str, Any]:
    if not isinstance(out, dict):
        return _unavailable(name, "bad_mcp_payload")
    status = str(out.get("status") or "")
    if status in {"unavailable", "error"} or out.get("ok") is False:
        reason = str(out.get("reason") or out.get("error") or status or "unavailable")
        speak = str(out.get("speak") or "I can't reach that service right now.")
        return {
            "ok": False,
            "name": name,
            "status": "unavailable",
            "reason": reason,
            "speak": speak,
            "result": out,
        }
    speak = str(out.get("speak") or out.get("summary") or "").strip()
    if not speak:
        events = out.get("events") or out.get("results") or out.get("files") or []
        if isinstance(events, list) and events:
            bits = []
            for ev in events[:3]:
                if isinstance(ev, dict):
                    bits.append(
                        str(ev.get("title") or ev.get("subject") or ev.get("name") or "")[:80]
                    )
            speak = "; ".join(b for b in bits if b) or "I found a few items."
        else:
            speak = "Done."
    return _ok(name, speak, out)
