"""The only code path allowed to invoke a tool.

Callers: nova_hailo.tools.oem_tools (facade). Nothing else may call
mcp_web_search / mcp_deep_research / mcp_research_status / the Workspace MCP
tool classes directly -- everything routes through ToolBroker.execute().

Moved from OemToolGateway.execute()/_web_search()/_ensure_workspace_auth()/
_mcp_calendar()/_mcp_gmail()/_mcp_drive()/_normalize_mcp()/poll_research()
verbatim; dispatch is now against typed Intent instead of a bare string
match chain. Returns plain dicts (not ToolResult) for pipeline.py, which
still consumes {ok,name,status,speak,result}; the three search_mcp.py
functions already return that exact shape, so their dicts pass through
unwrapped rather than being re-boxed.
"""
from __future__ import annotations

import os
import re
from typing import Any

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.result_compressor import speak_summary
from nova_hailo.edge_harness.router import (
    CAPABILITY_SPEAK,
    IDENTITY_SPEAK,
    _smalltalk_speak,
    _unavailable_cap_speak,
)
from nova_hailo.edge_harness.types import Intent, RoutedIntent, ToolResult
from nova_hailo.tools.search_mcp import deep_research as mcp_deep_research
from nova_hailo.tools.search_mcp import research_status as mcp_research_status
from nova_hailo.tools.search_mcp import web_search as mcp_web_search


# Leading ASR disfluencies stripped before list-all match (not from search needles).
_DRIVE_ASR_FILLER_RE = re.compile(
    r"""(?ix)
    ^(?:
      (?:like|uh|um|so|well|okay|ok|hey|and)
      [\s,]+
    )+
    """
)

# List-all Drive phrasings: empty query → list_recent_files (not title search).
_DRIVE_LIST_ALL_RE = re.compile(
    r"""(?ix)
    ^\s*
    (?:(?:can|could|would)\s+you\s+|please\s+)*
    (?:
      # what's in/on my drive
      (?:what(?:'s|\s+is)|whats)\s+(?:in|on)\s+(?:my\s+)?(?:google\s+)?drive
      |
      # show/list my drive
      (?:show|list|open|get|see|check|browse|display)\s+(?:me\s+)?(?:my\s+)?(?:google\s+)?drive
      |
      # bare drive
      (?:my\s+)?(?:google\s+)?drive
      |
      # (show|list|what are|…) (my|all|the|recent)* (drive)? files/folders/docs (in my drive)?
      # Live gap: "what are the files and folders in my drive" must list-all.
      (?:
        (?:show|list|open|get|see|check|browse|display)(?:\s+me)?
        |
        what(?:'s|\s+are|\s+is)|whats
      )?\s*
      (?:all\s+|my\s+|the\s+|recent\s+|any\s+)*
      (?:google\s+)?(?:drive\s+)?
      (?:
        files?(?:\s+and\s+folders?)?
        |folders?
        |documents?
        |docs?
      )
      (?:\s+(?:in|on|from|of)\s+(?:my\s+)?(?:google\s+)?drive)?
    )
    \s*[?.!]?\s*$
    """,
)


def drive_list_query(query: str) -> str:
    """Normalize LIST_DRIVE_FILES query for Drive MCP.

    List-all asks ("my drive", "files and folders in my drive", "list files",
    "what's in my drive", …) become "" so the tool lists recent files instead
    of title-searching the full ASR sentence (which yields "No Drive files
    matching …"). Specific search asks pass through unchanged.

    Leading ASR fillers (like/uh/um/so/well/okay/ok/hey/and + optional commas)
    are stripped only for list-all matching; non-list-all queries return the
    original stripped string so real search needles are unchanged.
    """
    q = (query or "").strip()
    if not q:
        return ""
    q_match = _DRIVE_ASR_FILLER_RE.sub("", q, count=1).strip()
    if _DRIVE_LIST_ALL_RE.match(q_match or q):
        return ""
    return q


_WRITE_INTENTS = frozenset({"create_calendar_event", "delete_calendar_event", "send_email"})


def _unavailable(name: str, reason: str) -> dict[str, Any]:
    return ToolResult(
        ok=False, name=name, status="unavailable", speak="I can't reach that service right now.", reason=reason
    ).to_dict()


def _ok(name: str, speak: str, result: Any = None) -> dict[str, Any]:
    return ToolResult(ok=True, name=name, status="success", speak=speak, result=result).to_dict()


def _normalize_mcp(name: str, out: Any) -> dict[str, Any]:
    if not isinstance(out, dict):
        return _unavailable(name, "bad_mcp_payload")
    status = str(out.get("status") or "")
    if status in {"unavailable", "error"} or out.get("ok") is False:
        reason = str(out.get("reason") or out.get("error") or status or "unavailable")
        speak = str(out.get("speak") or "I can't reach that service right now.")
        return ToolResult(ok=False, name=name, status="unavailable", speak=speak, result=out, reason=reason).to_dict()
    return _ok(name, speak_summary(out), out)


class ToolBroker:
    """Fail-closed adapters for the OEM allowlist tool family."""

    def __init__(
        self,
        profile: CapabilityProfile,
        *,
        timeout_sec: float = 1.5,
        serper_fallback: bool = True,
    ):
        self.profile = profile
        self.timeout_sec = float(timeout_sec)
        self.serper_fallback = bool(serper_fallback)

    def execute(self, routed: RoutedIntent) -> dict[str, Any]:
        intent, query, slots = routed.intent, routed.query, routed.slots
        name = intent.value
        if intent is Intent.IDENTITY:
            return _ok(name, IDENTITY_SPEAK)
        if intent is Intent.CAPABILITY:
            return _ok(name, CAPABILITY_SPEAK)
        if intent is Intent.SMALLTALK:
            # Router may supply a canned speak (e.g. system-prompt refuse) via slots.
            speak = str(slots.get("speak") or "") or _smalltalk_speak(query)
            return _ok(name, speak) if speak else _unavailable(name, "no_smalltalk_match")
        if intent is Intent.CAPABILITY_UNAVAILABLE:
            speak = _unavailable_cap_speak(query, self.profile.enabled)
            return _ok(name, speak) if speak else _unavailable(name, "no_capability_match")
        if not self.profile.allows(intent):
            return _unavailable(name, "tool_not_in_oem_allowlist")
        if name in _WRITE_INTENTS and not self.profile.write_enabled:
            return _unavailable(name, "writes_disabled_for_oem_demo")
        if intent is Intent.WEB_SEARCH:
            return self._web_search(query)
        if intent is Intent.DEEP_RESEARCH:
            return mcp_deep_research(query, timeout_sec=max(self.timeout_sec, 12.0))
        if intent is Intent.RESEARCH_STATUS:
            return mcp_research_status(str(slots.get("job_id") or ""))
        if intent is Intent.CHECK_CALENDAR:
            return self._mcp_calendar(query, str(slots.get("day") or "week"))
        if intent is Intent.CHECK_EMAIL:
            return self._mcp_gmail(query)
        if intent is Intent.LIST_DRIVE_FILES:
            return self._mcp_drive(query)
        return _unavailable(name, "unknown_tool")

    def poll_research(self, job_id: str) -> dict[str, Any]:
        return mcp_research_status(job_id)

    def _web_search(self, query: str) -> dict[str, Any]:
        return mcp_web_search(query, timeout_sec=self.timeout_sec, serper_fallback=self.serper_fallback)

    def _ensure_workspace_auth(self) -> dict[str, Any] | None:
        """Require one-time UI/CLI OAuth tokens stored by nova_hailo.google_oauth."""
        try:
            from nova_hailo.google_oauth import GoogleTokenProvider
        except Exception as exc:  # noqa: BLE001
            return _unavailable("workspace", f"oauth_import:{exc}")
        provider = GoogleTokenProvider()
        if not provider.configured():
            return _unavailable("workspace", "oauth_not_configured")
        if not provider.authenticated():
            return _unavailable("workspace", "oauth_not_connected")
        # Point any optional MCP adapters at the same token file
        os.environ.setdefault("GOOGLE_OAUTH_TOKEN_PATH", str(provider.store.path))
        return None

    def _mcp_calendar(self, query: str, day: str) -> dict[str, Any]:
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
            out = CheckCalendarTool().execute(day=day)
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
            # CheckEmailTool.execute(mode="unread"|...) -- no free-text query.
            out = CheckEmailTool().execute(mode="unread")
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
            out = ListDriveFilesTool().execute(query=drive_list_query(query))
            return _normalize_mcp("list_drive_files", out)
        except Exception as exc:  # noqa: BLE001
            return _unavailable("list_drive_files", f"mcp_call:{exc}")
