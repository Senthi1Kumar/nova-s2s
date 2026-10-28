"""System routes: health, models, metrics, streams, inspector, clients, governor."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .deps import get_services, set_meta

router = APIRouter()
_STARTED = time.time()
_KEEPALIVE_S = 15.0


@router.get("/health")
async def health(services=Depends(get_services)):
    loaded = [m["id"] for m in services.governor.models() if m.get("state") == "resident"]
    return {"status": "ok", "uptime_s": round(time.time() - _STARTED, 1), "models_loaded": loaded}


@router.get("/v1/models")
async def list_models(services=Depends(get_services)):
    return {"object": "list", "data": services.governor.models()}


@router.get("/v1/system/metrics")
async def metrics(request: Request):
    return await request.app.state.telemetry.current(max_age=1.5)


@router.websocket("/v1/system/stream")
async def system_stream(ws: WebSocket):
    await ws.accept()
    services = ws.app.state.services
    telemetry = ws.app.state.telemetry
    sub = services.inspector.subscribe()

    async def snapshots():
        while True:
            await ws.send_json({"kind": "snapshot", **(await telemetry.current(max_age=1.5))})
            await asyncio.sleep(1.0)

    async def events():
        async for e in sub:
            if e.get("kind") == "event":
                await ws.send_json(e)

    async def watch_disconnect():
        while True:
            await ws.receive_text()  # raises WebSocketDisconnect

    tasks = [asyncio.create_task(c()) for c in (snapshots, events, watch_disconnect)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await t
        await sub.aclose()


@router.get("/v1/inspector")
async def inspector_sse(request: Request, services=Depends(get_services)):
    sub = services.inspector.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(anext(sub), _KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(e, default=str)}\n\n"
        finally:
            await sub.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/inspector/recent")
async def inspector_recent(n: int = 200, services=Depends(get_services)):
    return {"object": "list", "data": services.inspector.recent(max(0, min(n, 2000)))}


@router.get("/v1/clients")
async def clients(services=Depends(get_services)):
    return {"object": "list", "data": services.inspector.clients()}


class GenaiSwitch(BaseModel):
    model: str | None = None


@router.post("/v1/governor/genai")
async def governor_genai(
    body: GenaiSwitch, request: Request, services=Depends(get_services)
):
    if not body.model:
        raise HTTPException(400, "'model' is required")
    t0 = time.perf_counter()
    try:
        await services.governor.switch_genai(body.model)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"unknown GenAI model: {body.model} ({e})") from e
    ms = (time.perf_counter() - t0) * 1000
    set_meta(request, {"model": body.model, "device": "npu", "queue_ms": 0.0,
                       "infer_ms": round(ms, 2)})
    return {"status": "ok", "model": body.model, "owner": services.governor.genai_owner(),
            "switch_ms": round(ms, 1)}
