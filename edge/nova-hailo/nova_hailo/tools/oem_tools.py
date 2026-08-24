"""OEM allowlist tool gateway: websearch FastMCP + optional Workspace MCP.

Callers: nova_hailo.pipeline (allowlist router).
v0.0.1 is conversation-only (profile: conversation, enabled: []). web_search
and deep_research (Brave->Serper; Tavily async) are built and tested here but
gated off; they return in v0.0.2.
API: OemToolGateway.route_and_execute(query)->{ok,name,speak,status,result}; never invents results.

Routing/execution logic lives in nova_hailo.edge_harness
(types.py/policy.py/router.py/tool_broker.py/result_compressor.py); this
class is a thin facade preserving the exact dict/string-returning public
API pipeline.py and tests/test_oem_demo_offline.py depend on.
"""
from __future__ import annotations

from typing import Any

from nova_hailo.edge_harness.policy import OEM_READONLY, WEBSEARCH_READONLY, CapabilityProfile
from nova_hailo.edge_harness.task_state import TaskState, apply_routed_turn
from nova_hailo.edge_harness.router import (
    _SMALLTALK,  # noqa: F401 -- re-exported for test_persona_drift.py
    _UNAVAILABLE_CAPS,  # noqa: F401 -- re-exported for test_persona_drift.py
    CAPABILITY_SPEAK,
    IDENTITY_SPEAK,
    calendar_day_slot,
    is_search_again,
)
from nova_hailo.edge_harness.router import route as _route
from nova_hailo.edge_harness.tool_broker import ToolBroker
from nova_hailo.edge_harness.types import Intent, RoutedIntent, ToolResult

# Real tools whose successful execution updates session last-tool memory.
# Identity / smalltalk / capability / clarify are excluded.
_MEMORY_INTENTS = frozenset(
    {
        Intent.WEB_SEARCH,
        Intent.DEEP_RESEARCH,
        Intent.CHECK_CALENDAR,
        Intent.CHECK_EMAIL,
        Intent.LIST_DRIVE_FILES,
    }
)
_SEARCH_RERUN_INTENTS = frozenset({Intent.WEB_SEARCH.value, Intent.DEEP_RESEARCH.value})

__all__ = [
    "CAPABILITY_SPEAK",
    "IDENTITY_SPEAK",
    "OEM_READONLY",
    "WEBSEARCH_READONLY",
    "OemToolGateway",
    "is_search_again",
]


class OemToolGateway:
    """Deterministic allowlist router + fail-closed adapters.

    Facade over nova_hailo.edge_harness: route() returns a bare string
    (or None), execute()/route_and_execute() return plain dicts, matching
    what pipeline.py and existing tests already expect.
    """

    def __init__(
        self,
        enabled: list[str] | None = None,
        *,
        timeout_sec: float = 1.5,
        write_enabled: bool = False,
        serper_fallback: bool = False,
    ):
        self.profile = CapabilityProfile.from_list(enabled, write_enabled=write_enabled)
        self.enabled = set(self.profile.enabled)
        self.timeout_sec = float(timeout_sec)
        self.write_enabled = bool(write_enabled)
        self.serper_fallback = bool(serper_fallback)
        self._broker = ToolBroker(
            self.profile, timeout_sec=self.timeout_sec, serper_fallback=self.serper_fallback
        )
        # Session memory for stateful "search again" / pure meta re-run.
        self._last_tool_intent: str | None = None
        self._last_tool_query: str | None = None
        # Host-owned compact task state (Hailo KV is never source of truth).
        self.task_state = TaskState()
        self.last_clear_llm = False

    def route(self, query: str) -> str | None:
        routed = _route(query, self.profile)
        return routed.intent.value if routed is not None else None

    def route_and_execute(self, query: str) -> dict[str, Any] | None:
        # Intercept search-again / pure meta before route() (which returns None).
        rerun = self._search_again_routed(query)
        if rerun is not None:
            result = self._broker.execute(rerun)
            self._maybe_store_last_tool(rerun, result)
            self._apply_task_state(query, rerun, result, search_again=True)
            return result

        routed = _route(query, self.profile)
        if routed is None:
            self.last_clear_llm = False
            return None
        result = self._broker.execute(routed)
        self._maybe_store_last_tool(routed, result)
        self._apply_task_state(query, routed, result, search_again=False)
        return result

    def execute(self, name: str, **kwargs: Any) -> dict[str, Any]:
        try:
            intent = Intent(name)
        except ValueError:
            return ToolResult(
                ok=False,
                name=name,
                status="unavailable",
                speak="I can't reach that service right now.",
                reason="unknown_tool",
            ).to_dict()
        query = str(kwargs.get("query") or "")
        slots: dict[str, Any] = {}
        if intent is Intent.RESEARCH_STATUS:
            slots["job_id"] = kwargs.get("job_id") or ""
        elif intent is Intent.CHECK_CALENDAR:
            slots["day"] = calendar_day_slot(query)
        return self._broker.execute(RoutedIntent(intent, query, slots))

    def poll_research(self, job_id: str) -> dict[str, Any]:
        return self._broker.poll_research(job_id)

    def _search_again_routed(self, query: str) -> RoutedIntent | None:
        """If query is search-again/pure-meta and last tool was search, re-run it."""
        if not is_search_again(query):
            return None
        if self._last_tool_intent not in _SEARCH_RERUN_INTENTS:
            return None
        last_q = (self._last_tool_query or "").strip()
        if not last_q:
            return None
        try:
            intent = Intent(self._last_tool_intent)
        except ValueError:
            return None
        return RoutedIntent(intent, last_q, {})

    def _maybe_store_last_tool(self, routed: RoutedIntent, result: dict[str, Any]) -> None:
        """Remember last successful real tool; skip identity/smalltalk/etc."""
        if not isinstance(result, dict) or not result.get("ok"):
            return
        if routed.intent not in _MEMORY_INTENTS:
            return
        self._last_tool_intent = routed.intent.value
        self._last_tool_query = routed.query

    def _apply_task_state(
        self,
        query: str,
        routed: RoutedIntent,
        result: dict[str, Any] | None,
        *,
        search_again: bool,
    ) -> None:
        compact = None
        ok = None
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            speak = str(result.get("speak") or "").strip()
            if speak:
                compact = speak[:240]
        nxt, clear = apply_routed_turn(
            self.task_state,
            user_text=query,
            new_intent=routed.intent.value,
            slots=dict(routed.slots or {}),
            search_again=search_again,
            compact_result=compact,
            tool_ok=ok,
        )
        self.task_state = nxt
        self.last_clear_llm = clear
        # Keep legacy last-tool fields aligned with host state.
        if nxt.last_tool:
            self._last_tool_intent = nxt.last_tool
            self._last_tool_query = nxt.last_query
