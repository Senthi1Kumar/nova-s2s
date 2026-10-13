"""FastAPI app: static orb UI + /v1/realtime + metrics."""
from __future__ import annotations

import asyncio
import os
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
from nova_hailo.web.public_url import public_ws_url
from nova_hailo.web.observe import HUB as OBSERVERS
from nova_hailo.web.session_control import CONTROL
from nova_hailo.web.observe import serve_observer
from nova_hailo.web.realtime_session import RealtimeSession
from nova_hailo.web.transport_auth import authorize as transport_authorize
from nova_hailo.web.transport_auth import configured_token
from nova_hailo.web.session_lease import SessionLease
from nova_hailo.wake_kws import WakeWordDetector
from nova_hailo.backends.nemo_speech_sidecar import ensure_sidecar, shutdown_sidecar

STATIC_DIR = Path(__file__).resolve().parent / "static"

_pipeline: NovaPipeline | None = None
_bench = BenchTracker(model_params_b=1.5)
_power = PowerMonitor(callback=_bench.record_power)
# One conversation across both front doors (/v1/realtime and LiveKit).
session_lease = SessionLease()
_livekit_door = None
_livekit_state = "disabled"


async def _start_livekit_door(pipeline: NovaPipeline) -> None:
    """Open the online door when configured. Never fails the LAN door."""
    global _livekit_door, _livekit_state
    try:
        from nova_hailo.lk.door import LiveKitDoor, door_config
    except ImportError:
        _livekit_state = "disabled"
        print("LiveKit: disabled (livekit extra not installed)", flush=True)
        return
    agent_name, reason = door_config(pipeline.cfg)
    if agent_name is None:
        _livekit_state = "disabled"
        print(f"LiveKit: {reason}", flush=True)
        return
    try:
        _livekit_door = LiveKitDoor(
            pipeline=pipeline,
            lease=session_lease,
            agent_name=agent_name,
            min_silence_s=float(pipeline.cfg.get("livekit", "min_silence_s", default=0.40)),
            endpoint_min_delay_s=float(
                pipeline.cfg.get("livekit", "endpoint_min_delay_s", default=0.30)
            ),
            wake_detector=_online_wake_detector(pipeline.cfg),
            followup_s=float(pipeline.cfg.get("livekit", "followup_s", default=5.0)),
            observers=OBSERVERS,
            session_control=CONTROL,
        )
        await _livekit_door.start()
        print(f"LiveKit: connecting as {agent_name}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the LAN door keeps serving
        _livekit_door, _livekit_state = None, "error"
        print(f"LiveKit: failed to start ({exc}); LAN realtime unaffected", flush=True)


def _online_wake_detector(cfg):
    """The online door's wake word: same wake.* model and threshold as the LAN.

    livekit.wake: kws (default) gates the room on "Hey Nova";
    livekit.wake: session keeps it always listening.
    """
    mode = str(cfg.get("livekit", "wake", default="kws") or "kws").strip().lower()
    if mode != "kws":
        print("LiveKit: wake word off (livekit.wake: session) -- always listening", flush=True)
        return None
    detector = WakeWordDetector.from_config(cfg)
    if not detector.available:
        print(
            "LiveKit: wake word unavailable (wake.kws_enabled / wake.kws_model) "
            "-- online door always listening",
            flush=True,
        )
        return None
    print(f"LiveKit: wake word on (threshold {detector.threshold})", flush=True)
    return detector


async def _stop_livekit_door() -> None:
    global _livekit_door
    if _livekit_door is not None:
        await _livekit_door.stop()
        _livekit_door = None


def _livekit_status() -> str:
    return _livekit_door.status if _livekit_door is not None else _livekit_state


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


def _gateway_cabin(pipeline: NovaPipeline | None):
    return getattr(getattr(pipeline, "oem_gateway", None), "cabin", None)


# Vehicle-delta sequence for /v1/observe (one counter per server run) and the
# touchscreen event policy; both exist only when the vehicle tool is enabled.
_vehicle_seq = [0]
_event_policy = None


def _start_vehicle_feed(pipeline: NovaPipeline):
    """Mirror every vehicle-state change to /v1/observe, with or without a voice session."""
    global _event_policy
    from nova_hailo.cabingym.events import vehicle_delta_event
    from nova_hailo.cabingym.hmi import EventPolicy

    OBSERVERS.bind(asyncio.get_running_loop())
    cabin = _gateway_cabin(pipeline)
    if cabin is None:
        return None
    _vehicle_seq[0] = 0

    def on_changes(changes) -> None:
        _vehicle_seq[0] += 1
        OBSERVERS.publish_vehicle_threadsafe(vehicle_delta_event(changes, _vehicle_seq[0]))

    def announce(text: str) -> None:
        # Spoken at the next quiet moment, on whichever door is connected.
        pipeline.task_registry.submit("announce", text, lambda h: h.complete(text))

    _event_policy = EventPolicy(cabin, pipeline.oem_gateway.vehicle, announce=announce)
    return cabin.store.subscribe(on_changes)


_sim_feed: list = [None]


def _start_sim_drive(pipeline: NovaPipeline):
    """Simulated drive + nova.nav (config cabingym.sim_drive); None when off."""
    from nova_hailo.cabingym.drive import build_nav_feed, drive_loop

    cabin = _gateway_cabin(pipeline)
    if cabin is None:
        return None
    feed = build_nav_feed(pipeline.cfg.get, cabin, publish=OBSERVERS.publish_vehicle_threadsafe)
    pipeline.nav_feed = feed
    if feed is None:
        return None
    _sim_feed[0] = feed
    cabin.store.subscribe(feed.on_changes)
    feed.start()
    feed.dirty = True  # publish the first nova.nav even when nothing is navigating
    print(f"CabinEnv sim drive: cruise {feed.drive.cruise_kph:.0f} km/h, "
          f"router {feed.routes.live_name if feed.routes.live else 'baked only'}"
          f"{', autoplay ' + '→'.join(feed.autoplay) if feed.autoplay else ''}", flush=True)
    return asyncio.create_task(drive_loop(feed, hz=5.0))


def _start_gps(pipeline: NovaPipeline):
    """Real GPS over NMEA serial (config cabingym.gps); None when off."""
    from nova_hailo.cabingym.adapters.gps_nmea import build_gps_feeder

    cabin = _gateway_cabin(pipeline)
    if cabin is None:
        return None
    feed = _sim_feed[0]
    gps = build_gps_feeder(pipeline.cfg.get, cabin,
                           simulating=(lambda: feed.drive.moving) if feed else (lambda: False))
    if gps is None:
        return None
    gps.start()
    print(f"CabinEnv GPS: {gps.device} at {gps.baud} baud, place lookup "
          f"{'mapbox' if gps.place_lookup else 'off'}", flush=True)
    return gps


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
    # Before either door: an online caller's job mirrors to /v1/observe from its own loop.
    OBSERVERS.bind(asyncio.get_running_loop())
    from nova_hailo.web.settings_ops import llm_settings

    OBSERVERS.publish_session_event({"type": "nova.settings", "llm": llm_settings(_pipeline)})
    await _start_livekit_door(_pipeline)
    unsub_vehicle = _start_vehicle_feed(_pipeline)
    drive_task = _start_sim_drive(_pipeline)
    gps = _start_gps(_pipeline)
    try:
        yield
    finally:
        if gps is not None:
            gps.stop()
        if drive_task is not None:
            drive_task.cancel()
        if unsub_vehicle is not None:
            unsub_vehicle()
        OBSERVERS.close()
        await _stop_livekit_door()
        _power.stop()
        shutdown_sidecar()
        if _pipeline:
            _pipeline.close()
            _pipeline = None
        stop_process_log()


app = FastAPI(title="Nova-Hailo Realtime", lifespan=lifespan)


@app.middleware("http")
async def _transport_auth_middleware(request: Request, call_next):
    """Gate every HTTP route on transport auth.

    Middleware rather than per-route Depends() on purpose: this is a security
    control, and a control you have to remember to attach to each new route is
    one that eventually gets forgotten. /static is exempt because it serves the
    page that then asks the user for a token.
    """
    if request.url.path.startswith("/static"):
        return await call_next(request)
    result = transport_authorize(
        client_host=request.client.host if request.client else None,
        authorization=request.headers.get("authorization"),
        query_token=request.query_params.get("token"),
        token=configured_token(),
    )
    if not result.allowed:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": {"type": result.decision.value, "message": result.reason}
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)

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
    instructions = ""
    soul = ROOT / "prompts" / "soul.md"
    if soul.exists():
        instructions = soul.read_text().strip()
    return {
        "instructions": instructions,
        "ws_url": public_ws_url(
            scheme=request.url.scheme,
            host_header=request.headers.get("host") or "",
            default_host=os.environ.get("NOVA_HAILO_PUBLIC_HOST") or "localhost",
            default_port=int(os.environ.get("NOVA_HAILO_PORT", "8766")),
            query_token=request.query_params.get("token"),
        ),
        "voices": ["amy-low"],
        "default_voice": "amy-low",
        "product": "nova-hailo",
        "livekit": _livekit_status(),
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


@app.post("/v1/vehicle/set")
async def vehicle_set(request: Request):
    """A touchscreen write: {path|alias, value} through the same gate as a voice command.

    Always answers 200 with {ok, ...}; ``seq`` is the vehicle-delta seq after the write.
    """
    from nova_hailo.cabingym.hmi import hmi_set

    gateway = getattr(_pipeline, "oem_gateway", None)
    cabin = getattr(gateway, "cabin", None)
    if cabin is None or getattr(gateway, "vehicle", None) is None:
        return {"ok": False, "error": "vehicle_disabled"}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad_json"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad_json"}
    out = hmi_set(cabin, gateway.vehicle, body, policy=_event_policy)
    out["seq"] = _vehicle_seq[0]
    return out


@app.get("/v1/session")
async def session_status():
    """Mic mute and the voice session's state, for displays."""
    from nova_hailo.web.session_control import CONTROL

    return CONTROL.status()


@app.post("/v1/session/mute")
async def session_mute(request: Request):
    """``{"muted": bool, "source": str}`` → ``{ok, muted}``. The mic only: Nova may finish."""
    from nova_hailo.web.session_control import CONTROL

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad_json"}
    muted = body.get("muted") if isinstance(body, dict) else None
    if not isinstance(muted, bool):
        return {"ok": False, "error": "bad_muted"}
    CONTROL.set_muted(muted, str(body.get("source") or "cockpit"))
    return {"ok": True, "muted": CONTROL.muted}


@app.post("/v1/session/arm")
async def session_arm():
    """Open the mic for a turn, like the wake word; refused while muted."""
    from nova_hailo.web.session_control import CONTROL

    return CONTROL.arm()


@app.post("/v1/session/cancel")
async def session_cancel():
    """× on a display: stop the reply and go idle."""
    from nova_hailo.web.session_control import CONTROL

    return CONTROL.cancel()


@app.get("/v1/settings")
async def settings_get():
    """The LLM choice for displays: provider, model, the providers to pick from, failover."""
    from nova_hailo.web.settings_ops import llm_settings

    if _pipeline is None:
        return {"ok": False, "error": "starting"}
    return {"llm": llm_settings(_pipeline)}


@app.post("/v1/settings")
async def settings_set(request: Request):
    """``{"llm": {"provider", "model"?}}`` → ``{ok, settings}``; observers get ``nova.settings``.

    Always 200, like /v1/vehicle/set; a refused choice says why and leaves the LLM as it was.
    """
    from nova_hailo.web.settings_ops import apply_llm_choice, llm_settings

    if _pipeline is None:
        return {"ok": False, "error": "starting"}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad_json"}
    ok, out = await apply_llm_choice(_pipeline, body)
    if not ok:
        return {"ok": False, "error": out, "settings": {"llm": llm_settings(_pipeline)}}
    settings = {"llm": out}
    OBSERVERS.publish_session_event({"type": "nova.settings", **settings})
    return {"ok": True, "settings": settings}


@app.websocket("/v1/observe")
async def observe(ws: WebSocket):
    """Read-only mirror of Nova's events for displays: no audio, no session lease."""
    auth = transport_authorize(
        client_host=ws.client.host if ws.client else None,
        authorization=ws.headers.get("authorization"),
        query_token=ws.query_params.get("token"),
        token=configured_token(),
    )
    await ws.accept()
    if not auth.allowed:
        await ws.send_json(
            {"type": "error", "error": {"type": auth.decision.value, "message": auth.reason}}
        )
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    def snapshot(seq: int):
        from nova_hailo.cabingym.events import vehicle_snapshot_event

        cabin = _gateway_cabin(_pipeline)
        if cabin is None:
            return None
        # Same shape as the session's seq-0 snapshot, tagged with the current seq.
        return {**vehicle_snapshot_event(cabin), "seq": seq}

    await serve_observer(ws, OBSERVERS, snapshot=snapshot)


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    # HTTP middleware does not run for the WebSocket scope, so the socket
    # carries its own check. Same accept-then-typed-error-then-close shape the
    # session limit below uses: a bare handshake rejection gives browser
    # clients no way to tell "wrong token" from "server down".
    auth = transport_authorize(
        client_host=ws.client.host if ws.client else None,
        authorization=ws.headers.get("authorization"),
        query_token=ws.query_params.get("token"),
        token=configured_token(),
    )
    if not auth.allowed:
        await ws.accept()
        await ws.send_json(
            {
                "type": "error",
                "error": {"type": auth.decision.value, "message": auth.reason},
            }
        )
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    lease = session_lease.try_acquire("realtime") if _pipeline is not None else None
    if lease is None:
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
    with lease:
        loop = asyncio.get_running_loop()
        session = RealtimeSession(ws, _pipeline, _bench, loop)
        OBSERVERS.session_started()
        try:
            await session.run()
        finally:
            OBSERVERS.session_ended()
