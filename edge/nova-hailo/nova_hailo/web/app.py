"""FastAPI app: static orb UI + /v1/realtime + metrics."""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager, redirect_stderr
from io import StringIO
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nova_hailo.bench import BenchTracker, PowerMonitor
from nova_hailo.config import ROOT, AppConfig
from nova_hailo.connectors import mcp_client
from nova_hailo.connectors import registry as connector_registry
from nova_hailo.google_oauth import (
    GoogleTokenProvider,
    begin_ui_oauth_flow,
    integration_status,
)
from nova_hailo.pipeline import NovaPipeline
from nova_hailo.session_log import start_process_log, stop_process_log
from nova_hailo.web.realtime_session import RealtimeSession
from nova_hailo.backends.nemo_speech_sidecar import ensure_sidecar, shutdown_sidecar

STATIC_DIR = Path(__file__).resolve().parent / "static"

_pipeline: NovaPipeline | None = None
_bench = BenchTracker(model_params_b=1.5)
_power = PowerMonitor(callback=_bench.record_power)
_session_lock = threading.Lock()
_active_sessions = 0
_MAX_SESSIONS = 1


def _build_pipeline() -> NovaPipeline:
    # Callers: lifespan / uvicorn. Env A/B for LLM+TTS.
    # Profile: NOVA_HAILO_PROFILE=oem|oem_rollback or NOVA_HAILO_CONFIG path.
    cfg = AppConfig.load()
    print(f"Config profile path: {cfg.path}")
    seq = os.environ.get("NOVA_HAILO_SEQUENTIAL_STT", "").lower()
    if seq in {"1", "true", "yes"}:
        cfg.raw.setdefault("pipeline", {})["sequential_stt"] = True
    elif seq in {"0", "false", "no"}:
        cfg.raw.setdefault("pipeline", {})["sequential_stt"] = False
    # else keep YAML (OEM: resident STT)

    if os.environ.get("NOVA_HAILO_LLM"):
        cfg.raw.setdefault("model", {})["llm_hef"] = os.environ["NOVA_HAILO_LLM"]
    if os.environ.get("NOVA_HAILO_TTS"):
        cfg.raw.setdefault("model", {})["tts_engine"] = os.environ["NOVA_HAILO_TTS"]
    if os.environ.get("NOVA_HAILO_STT"):
        cfg.raw.setdefault("model", {})["stt_engine"] = os.environ["NOVA_HAILO_STT"]
    one = os.environ.get("NOVA_HAILO_ONE_SENTENCE", "").lower()
    if one in {"1", "true", "yes"}:
        cfg.raw.setdefault("pipeline", {})["voice_one_sentence"] = True
    elif one in {"0", "false", "no"}:
        cfg.raw.setdefault("pipeline", {})["voice_one_sentence"] = False
    # else keep YAML (OEM: voice_one_sentence=true)

    # Env wins over config.yaml (config defaults to wm8960 for CLI).
    playback = (
        os.environ.get("NOVA_HAILO_PLAYBACK")
        or cfg.get("voice", "playback")
        or "browser"
    ).lower()
    cfg.raw.setdefault("voice", {})["playback"] = playback

    with redirect_stderr(StringIO()):
        pipe = NovaPipeline(
            cfg=cfg,
            llm_hef=os.environ.get("NOVA_HAILO_LLM"),
            whisper_hef=os.environ.get("NOVA_HAILO_WHISPER"),
            no_tts=False,
            text_only=False,
        )
    # Force after construct — NovaPipeline also reads voice.playback
    if playback == "browser":
        pipe.tts.local_play = False
    elif playback == "wm8960":
        pipe.tts.local_play = True
    else:  # both
        pipe.tts.local_play = True
    pipe.wait_tts_drain = True
    print(f"voice.playback={playback} → TTS local_play={pipe.tts.local_play}")
    return pipe


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipeline
    print("Initializing Nova-Hailo web pipeline…", flush=True)
    start_process_log()
    _pipeline = _build_pipeline()
    print(f"LLM: {_pipeline.llm.hef_path}", flush=True)
    if _pipeline.stt:
        print(f"STT resident [{_pipeline.stt_engine}]: {_pipeline.stt.hef_path}", flush=True)
    else:
        print("STT: on-demand (sequential_stt)", flush=True)
    print(f"TTS local_play={_pipeline.tts.local_play} stream_tts={_pipeline.stream_tts}", flush=True)
    if _pipeline.stt_engine == "nemo_speech":
        from nova_hailo.backends.nemo_speech_stt import (
            resolve_nemo_speech_paths,
            resolve_nemo_speech_vad_path,
        )

        model, _lib = resolve_nemo_speech_paths(
            _pipeline.cfg.get("model", "nemo_speech_model")
        )
        vad = resolve_nemo_speech_vad_path(_pipeline.cfg.get("model", "nemo_speech_vad_model"))
        if model:
            rnnt_rc = int(
                _pipeline.cfg.get("model", "nemo_rnnt_right_context", default=1) or 1
            )
            url = ensure_sidecar(
                str(model),
                str(vad) if vad else None,
                rnnt_right_context=rnnt_rc,
            )
            print(f"nemo_speech sidecar: {url or 'unavailable, live turns fall back to offline ctypes'}")
    _power.start()
    try:
        yield
    finally:
        _power.stop()
        shutdown_sidecar()
        if _pipeline:
            _pipeline.close()
            _pipeline = None
        stop_process_log()


app = FastAPI(title="Nova-Hailo Realtime", lifespan=lifespan)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return {"error": "UI missing", "path": str(index_path)}
    return FileResponse(index_path)


@app.get("/dashboard")
async def dashboard():
    """Glass-cockpit ops view: FSM, STT path, latency — not the driver face."""
    path = STATIC_DIR / "dashboard.html"
    if not path.exists():
        return {"error": "dashboard missing", "path": str(path)}
    return FileResponse(path)


@app.get("/config")
async def config(request: Request):
    port = int(os.environ.get("NOVA_HAILO_PORT", "8766"))
    # Prefer Host header so laptop→Pi demos get the right WS URL
    host_hdr = request.headers.get("host") or ""
    if host_hdr and ":" in host_hdr:
        public_host = host_hdr.rsplit(":", 1)[0]
        try:
            port = int(host_hdr.rsplit(":", 1)[1])
        except ValueError:
            pass
    elif host_hdr:
        public_host = host_hdr
    else:
        public_host = os.environ.get("NOVA_HAILO_PUBLIC_HOST") or "localhost"
    instructions = ""
    soul = ROOT / "prompts" / "soul.md"
    if soul.exists():
        instructions = soul.read_text().strip()
    return {
        "instructions": instructions,
        "ws_url": f"ws://{public_host}:{port}/v1/realtime",
        "voices": ["amy-low"],
        "default_voice": "amy-low",
        "product": "nova-hailo",
    }


@app.get("/metrics/system")
async def metrics_system():
    cpu = ram = None
    try:
        import psutil

        cpu = round(psutil.cpu_percent(interval=None), 1)
        ram = round(psutil.virtual_memory().percent, 1)
    except Exception:
        pass
    return {
        "cpu_pct": cpu,
        "ram_pct": ram,
        "power_w": _power.latest_w,
        "gpu_pct": None,
    }


@app.get("/metrics/bench")
async def metrics_bench():
    out = _bench.summary()
    # Audible TTFA lives on the pipeline's SessionMetrics, which until now was
    # only printed by the CLI path -- so the release-gate number was invisible
    # to the demo that actually produces it.
    if _pipeline is not None:
        summary = _pipeline.session.summary()
        out["ttfa"] = {k: v for k, v in summary.items() if k.startswith("ttfa")}
    return out


class GoogleAuthStartRequest(BaseModel):
    force: bool = False


@app.get("/integrations/google")
async def google_integration_status():
    """Settings pill: Google Workspace OAuth readiness (tokens on this Pi)."""
    return integration_status()


@app.post("/integrations/google/auth/start")
async def google_auth_start(req: GoogleAuthStartRequest = GoogleAuthStartRequest()):
    """Start localhost :8765 callback; return auth_url for window.open in this Chrome."""
    if req.force:
        GoogleTokenProvider().store.clear()
    try:
        return begin_ui_oauth_flow()
    except RuntimeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/integrations/google/disconnect")
async def google_disconnect():
    """Forget stored refresh tokens locally (does not revoke at Google)."""
    GoogleTokenProvider().store.clear()
    return {"status": "disconnected", "authenticated": False}


class ConnectorCreateRequest(BaseModel):
    label: str
    url: str
    kind: str = "mcp_http"


def _connectors_payload() -> dict:
    return {
        "connectors": connector_registry.list_connectors(),
        "max_connectors": connector_registry.MAX_CONNECTORS,
        "max_total_tools": connector_registry.MAX_TOTAL_TOOLS,
        "enabled_tool_total": connector_registry.total_enabled_tools(),
    }


def _refresh_tools_best_effort(cid: str, url: str) -> None:
    """Fetch a connector's live tool list so the UI can show a real count.

    Best-effort only: an offline or misconfigured MCP server must never
    block adding/enabling a connector. Never raises.
    """
    try:
        tools = mcp_client.list_tools(url)
    except Exception:
        return
    try:
        connector_registry.set_tools(cid, tools)
    except connector_registry.ConnectorError:
        pass


@app.get("/integrations/connectors")
async def connectors_list():
    """Settings panel: the user's own MCP/connector list. Not yet wired into
    the LLM tool schema -- see nova_hailo/connectors/registry.py."""
    return _connectors_payload()


@app.post("/integrations/connectors")
async def connectors_create(req: ConnectorCreateRequest):
    try:
        connector = connector_registry.add_connector(req.label, req.url, req.kind)
    except connector_registry.ConnectorError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    # uvicorn runs this app single-process/single-loop and the realtime
    # websocket session shares that same loop -- mcp_client makes a
    # synchronous httpx call, so it must run off-loop or a slow/unreachable
    # connector server stalls a live voice turn for up to its timeout.
    await asyncio.to_thread(_refresh_tools_best_effort, connector["id"], connector["url"])
    return _connectors_payload()


@app.post("/integrations/connectors/{cid}/enable")
async def connectors_enable(cid: str):
    connectors = connector_registry.list_connectors()
    existing = next((c for c in connectors if c.get("id") == cid), None)
    if existing is not None:
        # Refresh right before enabling -- the cap check below must use a
        # fresh tool count, not whatever was known when the connector was
        # added. Offloaded to a thread for the same reason as above: this is
        # a blocking network call on the same loop the voice session uses.
        await asyncio.to_thread(_refresh_tools_best_effort, cid, existing["url"])
    try:
        connector_registry.enable_connector(cid)
    except connector_registry.ConnectorNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    except connector_registry.ConnectorError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return _connectors_payload()


@app.post("/integrations/connectors/{cid}/disable")
async def connectors_disable(cid: str):
    try:
        connector_registry.disable_connector(cid)
    except connector_registry.ConnectorNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return _connectors_payload()


@app.delete("/integrations/connectors/{cid}")
async def connectors_delete(cid: str):
    ok = connector_registry.remove_connector(cid)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "connector not found"})
    return _connectors_payload()


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    global _active_sessions
    with _session_lock:
        if _active_sessions >= _MAX_SESSIONS or _pipeline is None:
            await ws.accept()
            await ws.send_json(
                {
                    "type": "error",
                    "error": {
                        "type": "session_limit_reached",
                        "message": "Only one realtime session supported",
                    },
                }
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        _active_sessions += 1
    try:
        loop = asyncio.get_running_loop()
        session = RealtimeSession(ws, _pipeline, _bench, loop)
        await session.run()
    finally:
        with _session_lock:
            _active_sessions = max(0, _active_sessions - 1)
