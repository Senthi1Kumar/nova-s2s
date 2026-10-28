"""FastAPI app factory + access middleware (auth, inspector rows, client table)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.deps import new_request_id
from .services import Services, SlotBusy
from .telemetry import Telemetry

log = logging.getLogger("aibox.app")
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
MAX_BODY = 26 * 1024 * 1024  # 25 MB upload + multipart overhead
MAX_VIDEO_BODY = 201 * 1024 * 1024  # 200 MB video upload + multipart overhead
VIDEO_UPLOAD_PATH = "/v1/vision/videos"


def _body_limit(scope) -> int:
    return MAX_VIDEO_BODY if scope.get("path") == VIDEO_UPLOAD_PATH else MAX_BODY


class BodyTooLarge(Exception):
    pass

_QUIET = {"/v1/inspector", "/v1/system/stream"}  # long-lived observers: no self-rows


def _is_api(path: str) -> bool:
    return path == "/health" or path.startswith("/v1/")


def _error(code: str, message: str, status: int, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


class AccessMiddleware:
    """Pure ASGI (BaseHTTPMiddleware would break SSE/WS cancellation)."""

    def __init__(self, app, services: Services, telemetry: Telemetry) -> None:
        self.app = app
        self.services = services
        self.telemetry = telemetry

    def _key(self, scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                val = v.decode("latin-1")
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
        q = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        return (q.get("key") or [None])[0]

    async def _fail(self, scope, snd, status, code, err, message) -> None:
        if scope["type"] == "http":
            if status["v"]:  # response already started: cannot change it, just record
                status["v"] = code
                return
            await _error(err, message, code)(scope, None, snd)
        else:
            status["v"] = code
            with contextlib.suppress(Exception):
                await snd({"type": "websocket.close", "code": 1011})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not _is_api(scope["path"]):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        client = scope.get("client")
        addr = client[0] if client else "unknown"
        headers = dict(scope.get("headers", []))
        ua = headers.get(b"user-agent", b"").decode("latin-1")
        rid = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        t0 = time.perf_counter()
        status = {"v": 0}
        counts = {"in": 0, "out": 0}
        client_id = addr

        api_key = self.services.settings.api_key
        authed = True
        if api_key and addr not in LOOPBACK:
            given = self._key(scope)
            authed = bool(given) and hmac.compare_digest(given.encode(), api_key.encode())
            if authed:
                client_id = "key:" + hashlib.sha256(given.encode()).hexdigest()[:8]

        async def recv():
            msg = await receive()
            if msg["type"] == "http.request":
                counts["in"] += len(msg.get("body", b""))
                if counts["in"] > _body_limit(scope):
                    raise BodyTooLarge()
            elif msg["type"] == "websocket.receive":
                counts["in"] += len(msg.get("bytes") or b"") + len((msg.get("text") or "").encode())
            return msg

        async def snd(msg):
            t = msg["type"]
            if t == "http.response.start":
                status["v"] = msg["status"]
            elif t == "http.response.body":
                counts["out"] += len(msg.get("body", b""))
            elif t == "websocket.accept":
                status["v"] = 101
            elif t == "websocket.close" and not status["v"]:
                status["v"] = 403
            elif t == "websocket.send":
                counts["out"] += len(msg.get("bytes") or b"") + len((msg.get("text") or "").encode())
            await send(msg)

        try:
            if not authed:
                if scope["type"] == "http":
                    resp = _error("unauthorized", "Missing or invalid API key", 401,
                                  {"WWW-Authenticate": "Bearer"})
                    await resp(scope, recv, snd)
                else:
                    await recv()
                    await snd({"type": "websocket.close", "code": 4401})
            elif scope["type"] == "http" and int(headers.get(b"content-length") or 0) > _body_limit(scope):
                await _error("payload_too_large", "request body larger than 25 MB", 413)(
                    scope, recv, snd)
            else:
                self.telemetry.note_request()
                try:
                    await self.app(scope, recv, snd)
                except BodyTooLarge:
                    await self._fail(scope, snd, status, 413, "payload_too_large",
                                     "request body larger than 25 MB")
                except Exception as e:  # noqa: BLE001 - backend failure -> JSON 500, never plain text
                    log.exception("unhandled error on %s", path)
                    await self._fail(scope, snd, status, 500, "internal",
                                     f"{type(e).__name__}: {e}")
        finally:
            if path not in _QUIET:
                meta = state.get("meta") or {}
                self.services.inspector.record({
                    "request_id": rid,
                    "client": client_id,
                    "endpoint": path,
                    "model": meta.get("model"),
                    "device": meta.get("device"),
                    "queue_ms": meta.get("queue_ms"),
                    "infer_ms": meta.get("infer_ms"),
                    "bytes_in": counts["in"],
                    "bytes_out": counts["out"],
                    "status": status["v"],
                    "total_ms": round((time.perf_counter() - t0) * 1000, 2),
                })
            if authed:
                self.services.inspector.touch_client(client_id, addr, ua)


def create_app(services: Services, static_dir: str | Path | None = None) -> FastAPI:
    telemetry = Telemetry(services)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(telemetry.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Hailo AI Box", version="0.1.0", lifespan=lifespan)
    app.state.services = services
    app.state.telemetry = telemetry
    app.add_middleware(AccessMiddleware, services=services, telemetry=telemetry)

    @app.exception_handler(SlotBusy)
    async def _slot_busy(request: Request, exc: SlotBusy):
        return _error("slot_busy", str(exc), 503,
                      {"Retry-After": str(max(1, round(exc.retry_after_s)))})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        msg = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        return _error(f"http_{exc.status_code}", msg, exc.status_code,
                      dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _error("invalid_request", json.dumps(exc.errors(), default=str)[:500], 400)

    _include_routers(app)

    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="studio")
    return app


def _include_routers(app: FastAPI) -> None:
    import importlib

    for name in ("system", "chat", "audio", "voice", "vision", "face"):
        try:
            mod = importlib.import_module(f"aibox.api.{name}")
        except ModuleNotFoundError as e:
            if e.name == f"aibox.api.{name}":
                continue
            raise
        app.include_router(mod.router)
