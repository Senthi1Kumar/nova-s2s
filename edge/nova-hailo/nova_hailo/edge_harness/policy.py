"""Capability profiles: which canonical intents a route may expose.

Callers: nova_hailo.edge_harness.tool_broker; nova_hailo.tools.oem_tools
(facade, re-exports OEM_READONLY/WEBSEARCH_READONLY for backward compat).
"""
from __future__ import annotations

from dataclasses import dataclass

from nova_hailo.edge_harness.types import Intent

OEM_READONLY: tuple[str, ...] = ("check_calendar", "check_email", "list_drive_files", "web_search")
WEBSEARCH_READONLY: tuple[str, ...] = ("web_search", "deep_research")

_WRITE_INTENTS = frozenset({"create_calendar_event", "delete_calendar_event", "send_email"})


@dataclass(frozen=True)
class CapabilityProfile:
    """Which tool intents a route may invoke, and whether writes are allowed.

    `enabled=[]` must mean "no tools", not "all tools" -- an empty list is
    falsy, so `enabled or OEM_READONLY` would silently enable everything in
    the conversation profile (measured 2026-07-30, oem_tools.py).
    """

    enabled: frozenset[str]
    write_enabled: bool = False

    @classmethod
    def from_list(
        cls, enabled: list[str] | None, *, write_enabled: bool = False
    ) -> CapabilityProfile:
        return cls(frozenset(OEM_READONLY if enabled is None else enabled), write_enabled)

    def allows(self, intent: Intent | str) -> bool:
        name = intent.value if isinstance(intent, Intent) else str(intent)
        return name in self.enabled

    def allows_write(self, intent: Intent | str) -> bool:
        name = intent.value if isinstance(intent, Intent) else str(intent)
        return self.write_enabled if name in _WRITE_INTENTS else True
