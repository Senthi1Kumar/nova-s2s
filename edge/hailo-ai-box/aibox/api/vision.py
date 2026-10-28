"""Vision: /v1/vision/detect, /v1/vision/ask, WS /v1/vision/stream."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import tempfile
import time

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from starlette.datastructures import UploadFile

from ..backends.post.facetrack import FaceSmoother
from ..media import MediaError, decode_image, encode_jpeg, to_rgb565
from .. import videos as videolib
from ..services import Services, SlotBusy
from .audio import MAX_UPLOAD
from .chat import VLM_IDS
from .deps import CAP_VLM, get_services, set_meta, timed_meta
from .wsutil import DISCONNECT, WsReader, aclose_quiet, send_error

router = APIRouter()
PREVIEW_MIN_INTERVAL_S = 0.1  # <= 10 preview fps
FACE_TRACK_PREVIEW_INTERVAL_S = 0.05  # the Face ID overlay wants a livelier preview (<= 20 fps)


async def _image_from_request(request: Request) -> tuple[np.ndarray, dict]:
    """Decode the image from multipart (`image`) or JSON (`image_b64`); also return other fields."""
    ctype = request.headers.get("content-type", "")
    fields: dict = {}
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        b64 = body.get("image_b64")
        if not b64:
            raise HTTPException(400, "'image_b64' is required")
        try:
            raw = base64.b64decode(b64.split(",", 1)[-1], validate=False)
        except (binascii.Error, ValueError, AttributeError) as e:
            raise HTTPException(400, f"bad base64: {e}") from e
        fields = body
    elif ctype.startswith("multipart/form-data"):
        form = await request.form()
        up = form.get("image")
        if not isinstance(up, UploadFile):
            raise HTTPException(400, "multipart field 'image' (file) is required")
        raw = await up.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "image larger than 25 MB")
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
    else:
        raise HTTPException(415, "send multipart/form-data (image) or JSON (image_b64)")
    try:
        img = await asyncio.to_thread(decode_image, raw)
    except MediaError as e:
        raise HTTPException(e.status, e.message) from e
    return img, fields


def _conf(v) -> float:
    try:
        c = float(v) if v is not None else 0.35
    except (TypeError, ValueError):
        raise HTTPException(400, "'conf' must be a number") from None
    if not 0.0 <= c <= 1.0:
        raise HTTPException(400, "'conf' must be within [0, 1]")
    return c


@router.post("/v1/vision/detect")
async def detect(request: Request, services: Services = Depends(get_services)):
    t0 = time.perf_counter()
    img, f = await _image_from_request(request)
    task = f.get("task") or "detection"
    if task not in services.vision.tasks():
        raise HTTPException(400, f"unknown task '{task}'; one of {services.vision.tasks()}")
    result = await services.vision.detect(img, task, _conf(f.get("conf")), f.get("model") or None)
    meta = set_meta(request, timed_meta(result.get("model", task), "npu", t0,
                                        float(result.get("inference_ms", 0.0))))
    return {**result, "meta": meta}


@router.post("/v1/vision/ask")
async def ask(request: Request, services: Services = Depends(get_services)):
    t0 = time.perf_counter()
    img, f = await _image_from_request(request)
    prompt = (f.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "'prompt' is required")
    model = f.get("model") or "qwen3-vl"
    if model not in VLM_IDS:
        raise HTTPException(404, f"model '{model}' not found")
    try:
        mt = min(int(f.get("max_tokens") or CAP_VLM), CAP_VLM)
    except (TypeError, ValueError):
        raise HTTPException(400, "'max_tokens' must be an integer") from None
    t1 = time.perf_counter()
    answer = await services.vlm.ask(img, prompt, model, max(1, mt))
    meta = set_meta(request, timed_meta(model, "npu", t0, (time.perf_counter() - t1) * 1000))
    return {"answer": answer, "meta": meta}


def _library(request_or_ws, services: Services) -> videolib.VideoLibrary:
    return videolib.library_for(request_or_ws.app.state, services.settings)


@router.get("/v1/vision/videos")
async def list_videos(request: Request, services: Services = Depends(get_services)):
    lib = _library(request, services)
    items = await asyncio.to_thread(lib.list)
    return {"data": [lib.public(v) for v in items]}


@router.post("/v1/vision/videos")
async def upload_video(request: Request, services: Services = Depends(get_services)):
    lib = _library(request, services)
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "send multipart/form-data with a 'file' field")
    form = await request.form()
    up = form.get("file")
    if not isinstance(up, UploadFile):
        raise HTTPException(400, "multipart field 'file' (video) is required")
    fd, tmp = tempfile.mkstemp(suffix=".upload", dir=await asyncio.to_thread(_ensure_dir, lib.upload_dir))
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await up.read(1024 * 1024):
                size += len(chunk)
                if size > videolib.MAX_VIDEO_BYTES:
                    raise HTTPException(413, "video larger than 200 MB")
                out.write(chunk)
        item = await asyncio.to_thread(lib.add_upload, tmp, up.filename or "video.mp4")
    except videolib.VideoError as e:
        raise HTTPException(e.status, e.message) from e
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return lib.public(item)


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


class _Latest:
    """Latest-wins single slot (a frame and, for video, its playback time)."""

    def __init__(self) -> None:
        self.frame: np.ndarray | None = None
        self.t: float | None = None
        self.taken_t: float | None = None
        self.ev = asyncio.Event()

    def put(self, frame: np.ndarray, t: float | None = None) -> None:
        self.frame = frame
        self.t = t
        self.ev.set()

    async def take(self) -> np.ndarray:
        await self.ev.wait()
        self.ev.clear()
        f, self.frame = self.frame, None
        self.taken_t = self.t
        assert f is not None
        return f


@router.websocket("/v1/vision/stream")
async def vision_stream(ws: WebSocket):
    await ws.accept()
    services: Services = ws.app.state.services
    reader = WsReader(ws)
    producer: asyncio.Task | None = None
    consumer: asyncio.Task | None = None
    control: asyncio.Task | None = None
    gens: list = []
    try:
        first = await reader.get()
        if first is DISCONNECT:
            return
        kind, cfg = first
        if kind != "json" or not isinstance(cfg, dict):
            await send_error(ws, "first message must be JSON {source, task, fps}")
            await ws.close(1003)
            return
        source = cfg.get("source")
        task = cfg.get("task") or "detection"
        if task not in services.vision.tasks():
            await send_error(ws, f"unknown task '{task}'", code="bad_task")
            await ws.close(1008)
            return
        video_item = None
        if source == "video":
            lib = _library(ws, services)
            video_item = await asyncio.to_thread(lib.get, str(cfg.get("video_id") or ""))
            if video_item is None:
                await send_error(ws, f"unknown video '{cfg.get('video_id')}'", code="bad_video")
                await ws.close(1008)
                return
        elif source != "client" and source not in services.cameras.sources():
            await send_error(ws, f"unknown source '{source}'", code="bad_source")
            await ws.close(1008)
            return
        state = {
            "task": task,
            "fps": float(cfg.get("fps") or 5),
            "preview": cfg.get("preview", "jpeg"),
            "preview_width": int(cfg.get("preview_width") or 640),
            "conf": float(cfg.get("conf") or 0.35),
            "model": cfg.get("model"),
        }
        if state["preview"] not in ("jpeg", "rgb565", "none"):
            await send_error(ws, "preview must be jpeg, rgb565 or none", code="bad_preview")
            await ws.close(1008)
            return
        latest = _Latest()
        async def pump_camera():
            if video_item is not None:
                agen = videolib.play(video_item["path"], state["fps"])
                gens.append(agen)
                async for frame, vt in agen:
                    latest.put(frame, vt)
                return
            agen = services.cameras.frames(source, state["fps"])
            gens.append(agen)
            async for frame in agen:
                latest.put(frame)

        async def pump_control():
            """Control updates, and uploaded frames for source=client."""
            while True:
                item = await reader.get()
                if item is DISCONNECT:
                    return
                k, v = item
                if k == "bytes" and source == "client":
                    try:
                        latest.put(await asyncio.to_thread(decode_image, v))
                    except MediaError as e:
                        await send_error(ws, e.message, code=f"http_{e.status}")
                elif k == "json" and isinstance(v, dict):
                    t = v.get("task")
                    if t in services.vision.tasks():
                        state["task"] = t
                    for key, cast in (("fps", float), ("preview_width", int), ("conf", float)):
                        with contextlib.suppress(TypeError, ValueError):
                            if v.get(key) is not None:
                                state[key] = cast(v[key])
                    if v.get("preview") in ("jpeg", "rgb565", "none"):
                        state["preview"] = v["preview"]

        async def process():
            frame_id = 0
            last_send = 0.0
            last_t0 = 0.0
            last_prev = 0.0
            ema_fps = 0.0
            smoother = FaceSmoother()
            while True:
                frame = await latest.take()
                cap = services.governor.vision_fps_cap()
                fps = min(state["fps"], cap) if cap else state["fps"]
                wait = last_t0 + 1.0 / max(fps, 0.1) - time.monotonic()  # period from the last frame's start: work overlaps the wait
                if wait > 0:
                    await asyncio.sleep(wait)
                    if latest.frame is not None:  # newer frame arrived while pacing
                        frame = await latest.take()
                t0 = last_t0 = time.monotonic()
                task_now = state["task"]
                missing = getattr(services.vision, "missing_models", lambda *_: [])(
                    task_now, state["model"])
                if missing:  # loading / evicted: say so instead of looking dead or failing
                    await ws.send_json({"type": "status", "state": "loading",
                                        "model": missing[0], "task": task_now})
                result = await services.vision.detect(frame, task_now, state["conf"],
                                                      state["model"])
                if task_now == "face_track":
                    dets = result.get("detections") or []
                    tracked = smoother.update(dets[0] if dets else None)
                    result = {**result, "detections": [tracked] if tracked else [],
                              "face": tracked}
                else:
                    smoother.reset()
                min_prev = (FACE_TRACK_PREVIEW_INTERVAL_S if task_now == "face_track"
                            else PREVIEW_MIN_INTERVAL_S)
                if state["preview"] != "none" and source != "client" \
                        and t0 - last_prev >= min_prev:
                    last_prev = t0
                    if state["preview"] == "jpeg":
                        await ws.send_bytes(await asyncio.to_thread(
                            encode_jpeg, frame, state["preview_width"]))
                    else:
                        data, w, h = await asyncio.to_thread(
                            to_rgb565, frame, state["preview_width"])
                        await ws.send_json({"type": "preview", "w": w, "h": h,
                                            "format": "rgb565"})
                        await ws.send_bytes(data)
                now = time.monotonic()
                if last_send:
                    inst = 1.0 / max(now - last_send, 1e-3)
                    ema_fps = inst if ema_fps == 0 else 0.7 * ema_fps + 0.3 * inst
                last_send = now
                frame_id += 1
                msg = {"type": "result", "frame_id": frame_id, "task": task_now,
                       "results": result, "fps": round(ema_fps or fps, 2)}
                if video_item is not None:
                    msg["video_t"] = latest.taken_t
                if services.governor.genai_state() == "generating":
                    msg["degraded"] = "genai_window"
                await ws.send_json(msg)

        if source != "client":
            producer = asyncio.create_task(pump_camera())
        control = asyncio.create_task(pump_control())
        consumer = asyncio.create_task(process())
        tasks = [t for t in (producer, control, consumer) if t]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if isinstance(exc, SlotBusy):
                await send_error(ws, str(exc), code="slot_busy",
                                 retry_after_s=exc.retry_after_s)
            elif exc is not None and not isinstance(exc, asyncio.CancelledError):
                await send_error(ws, f"{type(exc).__name__}: {exc}", code="internal")
    finally:
        for t in (producer, control, consumer):
            if t:
                t.cancel()
        for t in (producer, control, consumer):
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await reader.close()
        for g in gens:
            await aclose_quiet(g)
