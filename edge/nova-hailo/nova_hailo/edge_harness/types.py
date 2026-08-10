"""Canonical intents and typed routing/tool-result values.

Callers: nova_hailo.edge_harness.router, .tool_broker, .policy,
.result_compressor; nova_hailo.tools.oem_tools (facade).

Intent is a str-mixin Enum so `Intent.WEB_SEARCH == "web_search"` is True --
existing callers (pipeline.py, tests/test_oem_demo_offline.py) compare
route() output against bare strings, and this keeps that working through the
oem_tools facade without changing their assertions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Intent(str, Enum):
    IDENTITY = "identity"
    CAPABILITY = "capability"
    SMALLTALK = "smalltalk"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CHECK_CALENDAR = "check_calendar"
    CHECK_EMAIL = "check_email"
    LIST_DRIVE_FILES = "list_drive_files"
    WEB_SEARCH = "web_search"
    DEEP_RESEARCH = "deep_research"
    RESEARCH_STATUS = "research_status"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    DELETE_CALENDAR_EVENT = "delete_calendar_event"
    SEND_EMAIL = "send_email"


@dataclass(frozen=True)
class RoutedIntent:
    """Router output: a canonical intent + slots, not free-form tool JSON
    (SCENIC pattern, ROADMAP.md §2). `slots` holds anything the router could
    already resolve from the phrasing (e.g. check_calendar's day window) so
    the broker doesn't re-parse the query."""

    intent: Intent
    query: str
    slots: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Typed replacement for the {ok,name,status,speak,result,reason} dict
    OemToolGateway.execute() has always returned. to_dict() reproduces that
    exact shape for pipeline.py, which still consumes plain dicts."""

    ok: bool
    name: str
    status: str
    speak: str
    result: Any = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "name": self.name,
            "status": self.status,
            "speak": self.speak,
            "result": self.result,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out
