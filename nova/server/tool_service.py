"""Standalone FastAPI tool service: the seam between the Task 3/4 tool
inventory and the browser/s2s side.

``GET /tools/schemas`` returns every registered tool's ``to_function_tool()``
JSON (for the browser to put into ``session.update``). ``POST /tools/execute``
dispatches ``{"name", "args"}`` to the matching tool's ``execute()`` and
returns its result JSON.

Deliberately independent of the shelved edge track: this module imports only
from ``nova.tools.*`` and FastAPI/pydantic — never ``nova.engine`` or
``nova.server.app``/``nova.server.workers`` (per CLAUDE.md: those are the
LiteRT-LM edge track, not part of the demo path). A later task decides how
this router/app gets served alongside the browser UI.

Registry config-gating decision: the external tools (``WebSearchTool``,
``WeatherTool``, ``ResearchTool``) already self-degrade gracefully when their
API key is missing/broken (Tasks 3/4) — returning a clear "unavailable"
result rather than raising. So the registry always registers every tool;
"config-gated" behavior is delegated to each tool's own ``execute()``, not
duplicated here as a second gate.

Confirmation gate: ``ConfirmationGate`` wraps any ``NovaTool`` so its first
call (without ``confirmed=True``) returns
``{"status": "needs_confirmation", "prompt": ...}`` instead of acting, and
only performs the wrapped action when called again with ``confirmed=True``.
Wired for ``send_email``, ``create_calendar_event``, and
``delete_calendar_event``.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
import yaml
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nova.harness.jobs import JobManager
from nova.server.s2s_metrics import s2s_metrics
from nova.tools.agents import GetResearchResultTool, StartResearchTool
from nova.tools.base import NovaTool
from nova.tools.memory import MemoryDB, RecallMemoriesTool, RememberTool
from nova.tools.payment import DriveAuthGate, SendPaymentTool
from nova.tools.research import ResearchTool
from nova.tools.mcp.calendar import (
    CheckCalendarTool,
    CreateCalendarEventTool,
    DeleteCalendarEventTool,
)
from nova.tools.mcp.drive import CreateDriveFolderTool, ListDriveFilesTool
from nova.tools.mcp.gmail import CheckEmailTool
from nova.tools.stubs import (
    SendEmailStubTool,
    SpotifyStubTool,
)
from nova.tools.vehicle import VehicleDB, build_vehicle_tools
from nova.tools.weather import WeatherTool
from nova.tools.websearch import WebSearchTool

# Tool names that require a two-step confirmation before acting.
IRREVERSIBLE_TOOL_NAMES: set[str] = {
    "send_email",
    "create_calendar_event",
    "delete_calendar_event",
    "create_drive_folder",
}

_GATE_PROMPTS: dict[str, str] = {
    "send_email": "Are you sure you want to send this email?",
    "create_calendar_event": "Create this calendar event? Confirm the title and time.",
    "delete_calendar_event": "Delete this calendar event? Confirm which one.",
    "create_drive_folder": "Create this Drive folder? Confirm the name.",
}


def _gate_prompt(name: str) -> str | None:
    return _GATE_PROMPTS.get(name)


class ConfirmationGate(NovaTool):
    """Wraps a ``NovaTool`` so it needs an explicit ``confirmed=True`` before
    acting.

    First call (``confirmed`` omitted or false) returns
    ``{"status": "needs_confirmation", "prompt": ...}`` without touching the
    wrapped tool. A follow-up call with ``confirmed=True`` performs the real
    action and returns its result — so the assistant can speak the prompt and
    act on "yes" in a second turn.
    """

    def __init__(self, tool: NovaTool, prompt: str | None = None):
        self._tool = tool
        self.name = tool.name
        self.description = tool.description
        self.parameters = {
            **tool.parameters,
            "properties": {
                **tool.parameters.get("properties", {}),
                "confirmed": {
                    "type": "boolean",
                    "description": "Set to true only after the user has explicitly confirmed.",
                },
            },
        }
        self._prompt = prompt or f"Are you sure you want to {tool.name.replace('_', ' ')}?"

    def execute(self, confirmed: bool = False, **kwargs: Any) -> dict[str, Any]:
        if not confirmed:
            return {"status": "needs_confirmation", "prompt": self._prompt}
        return self._tool.execute(**kwargs)


def build_registry(
    db: VehicleDB | None = None,
    jobs: JobManager | None = None,
    memory: MemoryDB | None = None,
    driveauth_store: str | Path | None = None,
) -> dict[str, NovaTool]:
    """Assemble every enabled tool into a name -> ``NovaTool`` registry.

    ``db`` defaults to a real ``VehicleDB`` at ``runtime/vehicle.db``; ``jobs``
    defaults to a fresh ``JobManager``; ``memory`` defaults to a real
    ``MemoryDB`` at ``runtime/memory.db``. Tests pass temp-scoped instances of
    all three for isolation.
    """
    db = db if db is not None else VehicleDB()
    jobs = jobs if jobs is not None else JobManager()
    memory = memory if memory is not None else MemoryDB()
    research = ResearchTool()
    payment = DriveAuthGate(
        SendPaymentTool(),
        store_dir=str(driveauth_store or Path("runtime") / "driveauth_store"),
        driver_id=os.getenv("DRIVEAUTH_DRIVER_ID", "driver1"),
        use_mock_matchers=os.getenv("DRIVEAUTH_USE_MOCK", "1").strip().lower() not in {"0", "false", "off", "no"},
    )
    tools: list[NovaTool] = [
        *build_vehicle_tools(db),
        WebSearchTool(),
        WeatherTool(),
        research,
        CheckEmailTool(),
        CheckCalendarTool(),  # Google Calendar REST when OAuth present; else unavailable
        CreateCalendarEventTool(),
        DeleteCalendarEventTool(),
        ListDriveFilesTool(),
        CreateDriveFolderTool(),
        SpotifyStubTool(),
        StartResearchTool(jobs, research.execute),
        GetResearchResultTool(jobs),
        RememberTool(memory),
        RecallMemoriesTool(memory),
        SendEmailStubTool(),
        payment,
    ]
    gated = [
        ConfirmationGate(t, prompt=_gate_prompt(t.name))
        if t.name in IRREVERSIBLE_TOOL_NAMES
        else t
        for t in tools
    ]
    return {t.name: t for t in gated}


_default_registry: dict[str, NovaTool] | None = None
_default_jobs: JobManager | None = None


def get_jobs() -> JobManager:
    """FastAPI dependency: the process-wide default JobManager, built lazily."""
    global _default_jobs
    if _default_jobs is None:
        _default_jobs = JobManager()
    return _default_jobs


def get_registry() -> dict[str, NovaTool]:
    """FastAPI dependency: the process-wide default registry, built lazily.

    Tests override this via ``app.dependency_overrides[get_registry]`` to
    inject a temp-DB-backed registry instead of touching the real
    ``runtime/vehicle.db``.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = build_registry(jobs=get_jobs())
    return _default_registry


class ExecuteRequest(BaseModel):
    name: str
    args: dict[str, Any] = {}


class RouteRequest(BaseModel):
    query: str
    k: int = 8
    pinned: list[str] = []


class AgentRequest(BaseModel):
    query: str
    execute: bool = True
    session_id: str | None = None


class AuthPrecheckRequest(BaseModel):
    session_id: str
    transcript: str
    amount: float | None = None
    payee: str | None = None


class S2STurnMetricsRequest(BaseModel):
    """Per-turn stage timings posted by the s2s/realtime client (or tests).

    Unknown / null stages are ignored. Values are milliseconds except
    ``decode_tok_s`` (tokens/second).
    """

    session_id: str | None = None
    turn_id: str | None = None
    asr_ms: float | None = None
    driveauth_ms: float | None = None
    router_ms: float | None = None
    ttft_ms: float | None = None
    decode_tok_s: float | None = None
    tts_first_byte_ms: float | None = None
    ttfb_ms: float | None = None
    total_turn_ms: float | None = None
    # Allow a nested stages dict as an alternative to top-level fields.
    stages: dict[str, float | None] | None = None


# Tools awaiting user confirm / step-up. Merged into /tools/route so the
# server-side LLM override (no client pinned list) still forces the right tool
# on bare "Confirm." / "yes" — and ONLY on those (or a spoken PIN/OTP). Live
# LFM log: sticky send_payment after step_up trapped "research inference
# engines" and caused runaway meta-prompt speech.
_pending_confirm: set[str] = set()
_CONFIRM_STATUSES = frozenset({"needs_confirmation", "step_up_required"})
_CONFIRM_PIN_TOOLS = frozenset({
    "send_email",
    "send_payment",
    "create_calendar_event",
    "delete_calendar_event",
    "create_drive_folder",
})


def _is_step_up_reply(query: str) -> bool:
    """Spoken PIN/OTP after payment step_up — keep send_payment pinned."""
    import re

    digits = "".join(c for c in query if c.isdigit())
    if len(digits) >= 4:
        return True
    toks = set(re.findall(r"[a-z0-9]+", query.lower()))
    return bool(toks & {"pin", "otp", "code", "password"}) and "send_payment" in _pending_confirm


app = FastAPI(title="Nova Tool Service")

# Task 6: this same standalone app also serves the browser orb UI (Task 6's
# ``static/index.html``, self-contained HTML+CSS+JS, no build step) and its
# tiny ``/config`` prefill endpoint — kept here rather than in
# ``nova/server/app.py`` because that app is the shelved LiteRT-LM edge
# track (imports ``nova.engine`` and loads a real model at startup); this
# demo path must never touch it (see CLAUDE.md).
_STATIC_DIR = Path(__file__).parent / "static"
_SOUL_PATH = Path(__file__).parent.parent.parent / "prompts" / "soul.md"
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# Kokoro American-English voices exposed in the UI voice picker (a curated
# subset of the full Kokoro voice list — see the s2s kokoro_handler).
_VOICES = [
    "af_heart", "af_bella", "af_nova", "af_sarah",
    "am_michael", "am_eric", "am_adam", "am_liam",
]


def _ws_url_from_config() -> str:
    """Derive the realtime WebSocket URL the browser should connect to from
    ``nova/config.yaml`` (``ws_host``/``ws_port``), so the UI never hardcodes a
    port that can drift from what ``run_demo.py`` actually launches. Falls back
    to the s2s default if the config can't be read."""
    host, port = "127.0.0.1", 8765
    try:
        cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        host = cfg.get("ws_host", host)
        port = cfg.get("ws_port", port)
    except (OSError, yaml.YAMLError):
        pass
    return f"ws://{host}:{port}/v1/realtime"


@app.get("/")
def get_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/config")
def get_config() -> dict[str, Any]:
    """Prefill data for the browser UI: the Nova persona ("soul") prompt sent
    as ``session.update``'s ``instructions``, the realtime WS URL to connect to
    (derived from ``nova/config.yaml`` so the client never hardcodes the port),
    and the available TTS voices."""
    try:
        instructions = _SOUL_PATH.read_text()
    except OSError:
        instructions = ""
    return {
        "instructions": instructions,
        "ws_url": _ws_url_from_config(),
        "voices": _VOICES,
        "default_voice": _VOICES[0],
    }


class GoogleAuthStartRequest(BaseModel):
    force: bool = False


@app.get("/integrations/google")
def google_integration_status() -> dict[str, Any]:
    """Voice UI status pill: Google Workspace OAuth ready for Calendar (REST/MCP)."""
    from nova.tools.mcp.oauth import (
        DEFAULT_REDIRECT_URI,
        GOOGLE_WORKSPACE_SCOPES,
        GoogleTokenProvider,
        ui_oauth_status,
    )

    provider = GoogleTokenProvider()
    scopes: list[str] = []
    if provider.authenticated():
        data = provider.store.load() or {}
        raw = data.get("scopes") or []
        if isinstance(raw, list):
            scopes = [str(s) for s in raw]
    write_scope = "https://www.googleapis.com/auth/calendar.events"
    return {
        "configured": provider.configured(),
        "authenticated": provider.authenticated(),
        "project_id": provider.project_id() if provider.configured() else None,
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "scopes": scopes,
        "has_write": write_scope in scopes,
        "has_gmail": "https://www.googleapis.com/auth/gmail.readonly" in scopes,
        "has_drive": "https://www.googleapis.com/auth/drive.readonly" in scopes,
        "has_drive_write": "https://www.googleapis.com/auth/drive.file" in scopes,
        "requested_scopes": list(GOOGLE_WORKSPACE_SCOPES),
        "oauth_flow": ui_oauth_status(),
    }


@app.post("/integrations/google/auth/start")
def google_auth_start(req: GoogleAuthStartRequest = GoogleAuthStartRequest()) -> Any:
    """Start localhost OAuth callback; return auth_url for the UI to open in-tab.

    The UI must ``window.open(auth_url)`` so consent runs in the same Chrome
    profile as Nova — never the OS default browser. Pass ``{"force": true}`` to
    clear existing tokens and re-consent (e.g. upgrade readonly → write).
    """
    from nova.tools.mcp.oauth import GoogleTokenProvider, begin_ui_oauth_flow

    if req.force:
        GoogleTokenProvider().store.clear()
    try:
        return begin_ui_oauth_flow()
    except RuntimeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/integrations/google/disconnect")
def google_disconnect() -> dict[str, Any]:
    """Forget stored Google refresh tokens (does not revoke at Google)."""
    from nova.tools.mcp.oauth import GoogleTokenProvider

    GoogleTokenProvider().store.clear()
    return {"status": "disconnected", "authenticated": False}


def _gpu_stats() -> dict[str, Any]:
    """GPU utilization + memory via nvidia-smi. Returns nulls if unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip().splitlines()[0]
        util, mem_used, mem_total = (int(x.strip()) for x in out.split(","))
        return {"gpu_pct": util, "gpu_mem_used_mb": mem_used, "gpu_mem_total_mb": mem_total}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"gpu_pct": None, "gpu_mem_used_mb": None, "gpu_mem_total_mb": None}


@app.get("/metrics/system")
def get_system_metrics() -> dict[str, Any]:
    """Live host CPU / RAM / GPU usage for the UI's metric pills. ``cpu_pct``
    is instantaneous (non-blocking) so this endpoint is cheap to poll."""
    vm = psutil.virtual_memory()
    return {
        "cpu_pct": round(psutil.cpu_percent(interval=None)),
        "ram_pct": round(vm.percent),
        "ram_used_mb": round(vm.used / 1_048_576),
        "ram_total_mb": round(vm.total / 1_048_576),
        **_gpu_stats(),
    }


def _s2s_turn_stages(req: S2STurnMetricsRequest) -> dict[str, Any]:
    stages: dict[str, Any] = dict(req.stages or {})
    for key in (
        "asr_ms",
        "driveauth_ms",
        "router_ms",
        "ttft_ms",
        "decode_tok_s",
        "tts_first_byte_ms",
        "ttfb_ms",
        "total_turn_ms",
    ):
        value = getattr(req, key)
        if value is not None:
            stages[key] = value
    return stages


@app.post("/metrics/s2s/turn")
def record_s2s_turn(req: S2STurnMetricsRequest) -> dict[str, Any]:
    """Record one live s2s turn's stage timings into the M1 ring buffer."""
    record = s2s_metrics.record(
        _s2s_turn_stages(req),
        session_id=req.session_id,
        turn_id=req.turn_id,
    )
    return {
        "turn_id": record.turn_id,
        "session_id": record.session_id,
        "stages": record.stages,
        "turn_count": len(s2s_metrics),
    }


def _s2s_percentiles_payload(session_id: str | None = None) -> dict[str, Any]:
    return s2s_metrics.percentiles(session_id=session_id)


@app.get("/api/metrics/s2s/percentiles")
def get_s2s_percentiles_api(session_id: str | None = None) -> dict[str, Any]:
    """Per-turn + per-session p50/75/90/95/99 for the live s2s pipeline."""
    return _s2s_percentiles_payload(session_id)


@app.get("/metrics/s2s/percentiles")
def get_s2s_percentiles(session_id: str | None = None) -> dict[str, Any]:
    """Alias of ``/api/metrics/s2s/percentiles`` (matches ``/metrics/system``)."""
    return _s2s_percentiles_payload(session_id)


@app.post("/tools/auth/precheck")
def auth_precheck(req: AuthPrecheckRequest) -> dict[str, Any]:
    """DriveAuth first-layer gate (single owner in this process).

    Non-payment transcripts return ``status=bypass``. Payment turns return
    accept / step_up_required / denied. Never returns raw biometric payloads.
    """
    from nova.server.driveauth_bridge import precheck

    t0 = time.perf_counter()
    out = precheck(
        session_id=req.session_id,
        transcript=req.transcript,
        amount=req.amount,
        payee=req.payee,
    )
    driveauth_ms = (time.perf_counter() - t0) * 1000.0
    s2s_metrics.stash_stage(req.session_id, "driveauth_ms", driveauth_ms)
    if isinstance(out, dict):
        out = {**out, "driveauth_ms": round(driveauth_ms, 3)}
    return out


@app.get("/tools/schemas")
def get_schemas(registry: dict[str, NovaTool] = Depends(get_registry)) -> list[dict[str, Any]]:
    return [tool.to_function_tool() for tool in registry.values()]


@app.post("/tools/execute")
def execute_tool(
    req: ExecuteRequest, registry: dict[str, NovaTool] = Depends(get_registry)
) -> Any:
    tool = registry.get(req.name)
    if tool is None:
        return JSONResponse(status_code=404, content={"error": f"unknown tool: {req.name}"})
    args = dict(req.args)
    if req.name == "send_payment" and "session_id" not in args:
        # Optional: clients may put session_id on the request body later.
        pass
    try:
        result = tool.execute(**args)
    except TypeError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": f"invalid args for tool '{req.name}': {exc}"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": f"tool '{req.name}' failed: {exc}"},
        )
    if isinstance(result, dict) and result.get("status") in _CONFIRM_STATUSES:
        _pending_confirm.add(req.name)
    else:
        _pending_confirm.discard(req.name)
    return result


@app.post("/tools/route")
def route(req: RouteRequest, registry: dict[str, NovaTool] = Depends(get_registry)) -> dict:
    from nova.server.routing import _is_confirm_only, route_turn

    client_pinned = set(req.pinned)
    clear_pending = False
    if _is_confirm_only(req.query) or _is_step_up_reply(req.query):
        pinned = client_pinned | _pending_confirm
    else:
        # Topic change: drop sticky confirm/step-up pins (server + client).
        sticky = (client_pinned | _pending_confirm) & _CONFIRM_PIN_TOOLS
        if sticky:
            _pending_confirm.clear()
            clear_pending = True
        pinned = client_pinned - _CONFIRM_PIN_TOOLS
    out = route_turn(registry, req.query, req.k, pinned)
    out["clear_pending"] = clear_pending
    return out


@app.post("/tools/agent")
def tools_agent(
    req: AgentRequest, registry: dict[str, NovaTool] = Depends(get_registry)
) -> dict:
    """LFM2.5-230M tool pick + execute; speak LLM uses speak_instructions only."""
    from nova.server.tool_agent import run_tool_agent

    t0 = time.perf_counter()
    out = run_tool_agent(
        registry, req.query, execute=req.execute, session_id=req.session_id
    )
    router_ms = (time.perf_counter() - t0) * 1000.0
    if req.session_id:
        s2s_metrics.stash_stage(req.session_id, "router_ms", router_ms)
    # Mirror execute_tool pending-confirm bookkeeping for irreversible tools.
    for item in out.get("results") or []:
        name = item.get("name")
        result = item.get("result") or {}
        if not isinstance(name, str):
            continue
        if isinstance(result, dict) and result.get("status") in _CONFIRM_STATUSES:
            _pending_confirm.add(name)
        elif name in _CONFIRM_PIN_TOOLS:
            _pending_confirm.discard(name)
    out = {**out, "router_ms": round(router_ms, 3)}
    return out


@app.get("/jobs")
def list_jobs(jobs: JobManager = Depends(get_jobs)) -> list[dict[str, Any]]:
    return [
        {"id": j.id, "kind": j.kind, "status": j.status,
         "created_at": j.created_at, "finished_at": j.finished_at}
        for j in jobs.list()
    ]


@app.get("/jobs/announcements")
def job_announcements(jobs: JobManager = Depends(get_jobs)) -> list[dict[str, Any]]:
    """Finished-and-not-yet-announced background jobs, for the UI to speak
    proactively. Deliver-once: each job is only ever returned from here once
    (see ``JobManager.pending_announcements``)."""
    out = []
    for j in jobs.pending_announcements():
        if j.status == "failed":
            summary = f"the background {j.kind} failed: {j.error[:120]}"
        else:
            answer = (j.result or {}).get("answer") or (j.result or {}).get("note") or "finished"
            summary = str(answer)[:300]
        out.append({"id": j.id, "kind": j.kind, "status": j.status, "summary": summary})
    return out
