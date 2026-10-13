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

import logging
import os
import re
import threading
from typing import Any

from nova_hailo.cabingym.cloud_tools import PREFIX as VEHICLE_PREFIX
from nova_hailo.cabingym.cloud_tools import cloud_call
from nova_hailo.cabingym.confirm import parse_confirmation
from nova_hailo.cabingym.executor import VehicleExecutor
from nova_hailo.cabingym.grounding import apply_focus_zone
from nova_hailo.cabingym.router_jev import route_vehicle_jev, route_with_jev_vehicle
from nova_hailo.cabingym.router_regex import route_vehicle_regex
from nova_hailo.cabingym.runtime import CABIN_CONTROL, CabinEnv
from nova_hailo.cabingym.types import RouteOutcome
from nova_hailo.edge_harness import execution as _exec
from nova_hailo.edge_harness.jev_router import route_with_jev
from nova_hailo.edge_harness.policy import OEM_READONLY, WEBSEARCH_READONLY, CapabilityProfile
from nova_hailo.edge_harness.router import (
    _SMALLTALK,  # noqa: F401 -- re-exported for test_persona_drift.py
    _UNAVAILABLE_CAPS,  # noqa: F401 -- re-exported for test_persona_drift.py
    CAPABILITY_SPEAK,
    IDENTITY_SPEAK,
    calendar_day_slot,
    exact_routed,
    is_search_again,
)
from nova_hailo.edge_harness.router import route as _route
from nova_hailo.edge_harness.task_state import TaskState, apply_routed_turn
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
logger = logging.getLogger(__name__)


# Zone, colour, and number words that the device keyword lists do not cover on
# their own. "passenger side to twenty" has no "temperature" or "degrees".
_CABIN_HINT = re.compile(
    r"\b(driver|passenger|degrees?|red|blue|green|white|purple|orange|pink|"
    r"warmer|cooler|colder|hotter|folded|unfold\w*|eco|comfort|sport)\b|\d",
    re.I,
)


def _mentions_cabin(cabin: CabinEnv, text: str) -> bool:
    for name, dev in cabin.devices.items():
        if name in CABIN_CONTROL and re.search(dev.keywords, text or "", re.I):
            return True
    if _CABIN_HINT.search(text or ""):
        return True
    focus = cabin.focus or {}
    return bool(focus.get("device") in CABIN_CONTROL and parse_confirmation(text or ""))

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
        router: str = "regex",
        jev_timeout_sec: float = 0.45,
        jev_noul_min: float = 0.65,
        jev_max_actions: int = 2,
        jev_client: Any = None,
        jev_warm: bool = True,
        cabin: CabinEnv | None = None,
        tasks: Any = None,
        backend: Any = None,
        execution: str = "mixed",
        online: Any = None,
    ):
        self.profile = CapabilityProfile.from_list(enabled, write_enabled=write_enabled)
        self.enabled = set(self.profile.enabled)
        self.timeout_sec = float(timeout_sec)
        self.write_enabled = bool(write_enabled)
        self.serper_fallback = bool(serper_fallback)
        self.router = (router or "regex").strip().lower()
        self.jev_timeout_sec = float(jev_timeout_sec)
        self.jev_noul_min = float(jev_noul_min)
        self.jev_max_actions = int(jev_max_actions)
        # One keep-alive TypeSafeClient for the process (built lazily when not
        # injected). A client per call paid TCP+TLS setup on every routed turn.
        self._jev_client = jev_client
        self._jev_client_lock = threading.Lock()
        if (
            jev_warm
            and self.router == "jev"
            and jev_client is None
            and os.environ.get("TYPESAFE_API_KEY", "").strip()
        ):
            # Open the connection before the first turn needs it; listing
            # models costs no inference.
            threading.Thread(target=self._warm_jev_client, daemon=True).start()
        self.cabin: CabinEnv | None = None
        self.vehicle: VehicleExecutor | None = None
        if self.profile.allows(Intent.VEHICLE):
            self.cabin = cabin if cabin is not None else CabinEnv(live=True)
            self.vehicle = VehicleExecutor(self.cabin)
        self._broker = ToolBroker(
            self.profile, timeout_sec=self.timeout_sec, serper_fallback=self.serper_fallback,
            vehicle=self.vehicle,
        )
        # Harness (frontend/backend split). Without a task registry the gateway
        # behaves exactly as before: every parsed vehicle turn runs directly.
        self.tasks = tasks
        self.backend = backend
        self.execution = execution if tasks is not None else "direct"
        self._online = online or (lambda: True)
        # Session memory for stateful "search again" / pure meta re-run.
        self._last_tool_intent: str | None = None
        self._last_tool_query: str | None = None
        # Host-owned compact task state (Hailo KV is never source of truth).
        self.task_state = TaskState()
        self.last_clear_llm = False

    def route(self, query: str) -> str | None:
        exact = exact_routed(query)
        if exact is not None:
            return exact.intent.value
        jev = self._jev_routes(query)
        if jev:
            return jev[0].intent.value
        if self.cabin is not None and route_vehicle_regex(query, self.cabin) is not None:
            return Intent.VEHICLE.value
        routed = _route(query, self.profile)
        return routed.intent.value if routed is not None else None

    def route_and_execute(self, query: str) -> dict[str, Any] | None:
        confirmed = self._vehicle_confirmation(query)
        if confirmed is not None:
            return confirmed
        if self.vehicle is not None:
            self.vehicle.drop_pending()

        # Intercept search-again / pure meta before route() (which returns None).
        rerun = self._search_again_routed(query)
        if rerun is not None:
            return self._finish_one(query, rerun, search_again=True)

        exact = exact_routed(query)
        if exact is not None:
            return self._finish_one(query, exact, search_again=False)

        jev = self._jev_routes(query)
        if jev:
            if self.cabin is not None:
                jev_outcome = next(
                    (r.slots.get("outcome") for r in jev if r.intent is Intent.VEHICLE), None
                )
                delegated = self._maybe_delegate(query, jev_outcome)
                if delegated is not None:
                    return delegated
            return self._finish_many(query, jev)

        if self.cabin is not None:
            outcome = route_vehicle_regex(query, self.cabin)
            delegated = self._maybe_delegate(query, outcome)
            if delegated is not None:
                return delegated
            if outcome is not None:
                routed = RoutedIntent(Intent.VEHICLE, query, {"outcome": outcome})
                return self._finish_one(query, routed, search_again=False)

        routed = _route(query, self.profile)
        if routed is None:
            self.last_clear_llm = False
            return None
        return self._finish_one(query, routed, search_again=False)

    def _maybe_delegate(self, query: str, outcome: Any) -> dict[str, Any] | None:
        """Hand the turn to the backend agent, or None to keep it on the frontend."""
        if self.execution == "direct":
            return None
        online = self.backend is not None and bool(self._online())
        decision = _exec.decide(query, outcome, mode=self.execution, online=online)
        if decision.path not in (_exec.DELEGATE, _exec.OFFLINE):
            return None
        if decision.path == _exec.OFFLINE:
            result = ToolResult(ok=True, name="delegate", status="offline",
                                speak=_exec.OFFLINE_SPEAK,
                                result={"reason": decision.reason}).to_dict()
        else:
            backend = self.backend
            task, why = self.tasks.submit("delegate", query, lambda h: backend.run(query, h))
            if task is None:
                result = ToolResult(ok=True, name="delegate", status="busy",
                                    speak=_exec.BUSY_SPEAK, result={"reason": why}).to_dict()
            else:
                result = ToolResult(ok=True, name="delegate", status="accepted",
                                    speak=_exec.DELEGATE_ACK,
                                    result={"task_id": task.id,
                                            "reason": decision.reason}).to_dict()
        verdict = str(result["status"])
        result["result"]["trace"] = {
            "router": "backend",
            "calls": [{"device": "delegate", "api": "plan", "args": {"objective": query},
                       "status": verdict, "gate": {"verdict": verdict, "reason": None}}],
            "gate": [verdict], "diff": [], "speak": result["speak"],
        }
        self._apply_task_state(query, RoutedIntent(Intent.VEHICLE, query, {}), result,
                               search_again=False)
        return result

    def _vehicle_confirmation(self, query: str) -> dict[str, Any] | None:
        """A yes/no to a pending vehicle confirmation, before any other routing."""
        if self.vehicle is None or self.vehicle.pending is None:
            return None
        answer = parse_confirmation(query)
        if answer is None:
            return None
        result = self.vehicle.resolve_confirm(answer)
        if result is not None:
            self._apply_task_state(query, RoutedIntent(Intent.VEHICLE, query, {}), result,
                                   search_again=False)
        return result

    def _live_jev_client(self) -> Any:
        """The process-wide TypeSafeClient, or None when the SDK is missing."""
        with self._jev_client_lock:
            if self._jev_client is None:
                try:
                    from typesafe_sdk import RetryPolicy, TypeSafeClient
                except ImportError:
                    logger.info("jev_router:sdk_missing")
                    return None
                self._jev_client = TypeSafeClient(
                    retry=RetryPolicy(max_retries=0), timeout=self.jev_timeout_sec
                )
            return self._jev_client

    def _warm_jev_client(self) -> None:
        try:
            client = self._live_jev_client()
            if client is not None:
                client.models.list()
        except Exception as exc:  # noqa: BLE001 -- warming is best effort
            logger.info("jev_router:warm_failed %s", exc)

    def _jev_routes(self, query: str) -> list[RoutedIntent] | None:
        """Jev intents, or None so the caller uses the regex router."""
        if self.router != "jev":
            return None
        if self._jev_client is None and not os.environ.get("TYPESAFE_API_KEY", "").strip():
            logger.info("jev_router:fallback")
            return None
        client = self._live_jev_client()
        if client is None:
            return None
        if self.cabin is not None:
            return route_with_jev_vehicle(
                query, self.profile, self.task_state, self.cabin, client=client,
                timeout_sec=self.jev_timeout_sec, noul_min=self.jev_noul_min,
                max_actions=self.jev_max_actions,
            )
        return route_with_jev(
            query,
            self.profile,
            self.task_state,
            client=client,
            timeout_sec=self.jev_timeout_sec,
            noul_min=self.jev_noul_min,
            max_actions=self.jev_max_actions,
        )

    def _finish_one(
        self, query: str, routed: RoutedIntent, *, search_again: bool
    ) -> dict[str, Any]:
        result = self._broker.execute(routed)
        self._maybe_store_last_tool(routed, result)
        self._apply_task_state(query, routed, result, search_again=search_again)
        return result

    def _finish_many(self, query: str, routed_list: list[RoutedIntent]) -> dict[str, Any]:
        if len(routed_list) == 1:
            return self._finish_one(query, routed_list[0], search_again=False)
        parts: list[dict[str, Any]] = []
        speaks: list[str] = []
        for routed in routed_list:
            part = self._broker.execute(routed)
            parts.append(part)
            self._maybe_store_last_tool(routed, part)
            speak = str(part.get("speak") or "").strip()
            if speak:
                speaks.append(speak)
        primary = routed_list[0]
        joined = " ".join(speaks).strip()
        ok = any(bool(part.get("ok")) for part in parts)
        result = {
            "ok": ok,
            "name": "compound",
            "status": "success" if ok else str(parts[0].get("status") or "unavailable"),
            "speak": joined or str(parts[0].get("speak") or ""),
            "result": {"actions": parts},
        }
        self._apply_task_state(query, primary, result, search_again=False)
        return result

    def execute(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if name.startswith(VEHICLE_PREFIX):
            if self.vehicle is None or self.cabin is None:
                return ToolResult(ok=False, name=name, status="unavailable",
                                  speak="I can't reach that service right now.",
                                  reason="vehicle_not_configured").to_dict()
            outcome = apply_focus_zone(
                self.cabin, getattr(self, "_cabin_utterance", "") or "",
                cloud_call(name, kwargs, self.cabin),
            )
            result = self.vehicle.run(outcome)
            self._remember_vehicle(getattr(self, "_cabin_utterance", "") or "", outcome, result)
            return result
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

    def take_cabin_turn(self, utterance: str, dialog: list | None = None) -> dict[str, Any] | None:
        """Judge a cabin, climate, or media command with Jev, then execute it.

        Navigation is left for the cloud turn so Mapbox still resolves the
        place. A pure yes or no still resolves a pending confirmation. When
        Jev is unset or unsure, returns None and the cloud model proceeds.
        """
        self._cabin_utterance = utterance or ""
        if self.cabin is not None and dialog is not None:
            self.cabin.dialog = [dict(turn) for turn in dialog if isinstance(turn, dict)]
        confirmed = self._vehicle_confirmation(utterance)
        if confirmed is not None:
            return confirmed
        if self.router != "jev" or self.cabin is None or self.vehicle is None:
            return None
        if not _mentions_cabin(self.cabin, utterance):
            return None
        client = self._live_jev_client()
        if client is None:
            return None
        outcome = route_vehicle_jev(
            utterance, self.cabin, client=client, timeout_sec=self.jev_timeout_sec,
            noul_min=self.jev_noul_min, max_devices=2, dialog=self.cabin.dialog,
        )
        if outcome is None:
            return None
        if outcome.kind == "vehicle":
            kept = [call for call in outcome.calls if call.device in CABIN_CONTROL]
            if not kept:
                return None
            if len(kept) != len(outcome.calls):
                outcome = RouteOutcome("vehicle", kept, outcome.speak, dict(outcome.trace))
            outcome = apply_focus_zone(self.cabin, utterance, outcome)
        elif outcome.kind in {"clarify", "decline"}:
            scores = (outcome.trace or {}).get("stage1") or {}
            best = max(scores, key=scores.get) if scores else None
            if best not in CABIN_CONTROL:
                return None
        else:
            return None
        result = self.vehicle.run(outcome)
        self._remember_vehicle(utterance, outcome, result)
        return result

    def _remember_vehicle(self, utterance: str, outcome: RouteOutcome, result: dict[str, Any]) -> None:
        if self.cabin is None:
            return
        call = outcome.calls[0] if outcome.calls else None
        partial = (outcome.trace or {}).get("partial") or {}
        device = call.device if call is not None else partial.get("device")
        api = call.api if call is not None else partial.get("api") or ""
        args = call.args if call is not None else partial.get("args") or {}
        if not device:
            scores = (outcome.trace or {}).get("stage1") or {}
            device = max(scores, key=scores.get) if scores else None
        if not device:
            return
        self.cabin.remember_turn(
            utterance, str(device), str(api or ""), args,
            str(result.get("status") or outcome.kind), str(result.get("speak") or ""),
        )

    def maybe_rescue_vehicle(self, utterance: str, tool_name: str, out: dict[str, Any]) -> dict[str, Any]:
        """Run the regex vehicle route when the cloud call clarified on the wrong device.

        A fan request that the model sent to ``vehicle_navigation`` comes back as a
        clarify with an empty diff. The regex router already parses "set the fan
        speed to three". Same device with an action the model did choose is left
        alone: that clarify is the real missing slot, not a mis-route.
        """
        if self.vehicle is None or self.cabin is None:
            return out
        if not str(tool_name).startswith(VEHICLE_PREFIX):
            return out
        if str(out.get("status") or "") not in {"clarify", "declined", "decline"}:
            return out
        if ((out.get("result") or {}).get("diff") or []):
            return out
        text = (utterance or "").strip()
        if not text:
            return out
        outcome = route_vehicle_regex(text, self.cabin)
        if outcome is None or outcome.kind != "vehicle" or not outcome.calls:
            return out
        cloud_device = tool_name.removeprefix(VEHICLE_PREFIX)
        action = str(((out.get("result") or {}).get("trace") or {}).get("action") or "")
        if {call.device for call in outcome.calls} == {cloud_device} and action:
            return out
        rescued = self.vehicle.run(outcome)
        trace = dict((rescued.get("result") or {}).get("trace") or {})
        trace["rescued_from"] = tool_name
        if isinstance(rescued.get("result"), dict):
            rescued["result"]["trace"] = trace
        return rescued

    def poll_research(self, job_id: str) -> dict[str, Any]:
        return self._broker.poll_research(job_id)

    def execute_web_search_answer(
        self,
        query: str,
        *,
        system_prompt: str,
        on_token: Any = None,
    ) -> dict[str, Any]:
        """exa_direct_answer path (see nova_hailo.pipeline._openrouter_turn):
        Exa streams its own synthesized spoken answer via ``on_token``
        instead of returning raw hits for a second LLM round to compose
        from. Routes through ToolBroker like every other tool call."""
        return self._broker.web_search_answer(
            query, system_prompt=system_prompt, on_token=on_token
        )

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
            slots={k: v for k, v in (routed.slots or {}).items() if k != "outcome"},
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
