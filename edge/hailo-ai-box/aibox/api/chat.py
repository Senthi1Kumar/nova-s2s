"""Chat: OpenAI-shaped /v1/chat/completions (JSON + SSE) and WS /v1/chat/stream."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from ..media import MediaError, decode_image
from ..services import Services, SlotBusy
from .deps import CAP_CHAT, CAP_VLM, get_services, new_request_id, set_meta, timed_meta
from .wsutil import DISCONNECT, WsReader, aclose_quiet, send_error

router = APIRouter()
MAX_IMAGE_B64 = 12 * 1024 * 1024
VLM_IDS = {"qwen3-vl", "qwen2-vl"}
_DATA_URI = re.compile(r"^data:image/[\w.+-]+;base64,(.*)$", re.S)


@dataclass
class Generation:
    model: str
    device: str
    agen: AsyncIterator[str]
    t_start: float
    prompt_words: int


def _model_info(services: Services, model: str) -> dict:
    for m in services.governor.models():
        if m["id"] == model and m.get("modality") in ("chat", "vlm"):
            return m
    raise HTTPException(404, f"model '{model}' not found")


def split_messages(messages: list[dict]) -> tuple[list[dict], list[np.ndarray], str]:
    """OpenAI messages -> (text-only messages, decoded images, last user text)."""
    flat: list[dict] = []
    images: list[np.ndarray] = []
    last_user = ""
    for msg in messages:
        if not isinstance(msg, dict) or "role" not in msg:
            raise HTTPException(400, "each message needs a 'role'")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    mt = _DATA_URI.match(url)
                    if not mt:
                        raise HTTPException(400, "image_url must be a base64 data: URI")
                    if len(mt.group(1)) > MAX_IMAGE_B64:
                        raise HTTPException(413, "image_url data URI larger than 12 MB")
                    try:
                        raw = base64.b64decode(mt.group(1), validate=False)
                        images.append(decode_image(raw))
                    except (binascii.Error, ValueError) as e:
                        raise HTTPException(400, f"bad image data: {e}") from e
                    except MediaError as e:
                        raise HTTPException(e.status, e.message) from e
            content = "\n".join(texts)
        elif content is None:
            content = ""
        flat.append({"role": msg["role"], "content": str(content)})
        if msg["role"] == "user":
            last_user = flat[-1]["content"]
    return flat, images, last_user


async def _one(text: str) -> AsyncIterator[str]:
    yield text


async def start_generation(
    services: Services, model: str, messages: list[dict], max_tokens: int | None
) -> Generation:
    """Route to the VLM (image parts) or the chat service. May raise SlotBusy / HTTPException."""
    t0 = time.perf_counter()
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "'messages' must be a non-empty list")
    flat, images, last_user = await asyncio.to_thread(split_messages, messages)
    if images:
        vlm_model = model if model in VLM_IDS else "qwen3-vl"
        info = _model_info(services, vlm_model)
        cap = min(max_tokens or CAP_VLM, CAP_VLM)
        answer = await services.vlm.ask(images[-1], last_user or "Describe the image.",
                                        vlm_model, cap)
        return Generation(vlm_model, info.get("device", "npu"), _one(answer), t0,
                          len(last_user.split()))
    info = _model_info(services, model)
    cap = min(max_tokens or CAP_CHAT, CAP_CHAT)
    agen = services.chat.stream(model, flat, cap)
    words = sum(len(m["content"].split()) for m in flat)
    return Generation(model, info.get("device", "npu"), agen, t0, words)


def _usage(gen: Generation, n_tokens: int) -> dict:
    return {"prompt_tokens": gen.prompt_words, "completion_tokens": n_tokens,
            "total_tokens": gen.prompt_words + n_tokens}


def _int_or_none(v) -> int | None:
    try:
        return max(1, int(v)) if v is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "'max_tokens' must be an integer") from None


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, services: Services = Depends(get_services)):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "request body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    model = body.get("model")
    if not model:
        raise HTTPException(400, "'model' is required")
    gen = await start_generation(services, model, body.get("messages"),
                                 _int_or_none(body.get("max_tokens")))
    rid = request.state.request_id
    created = int(time.time())
    cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    agen = gen.agen

    if not body.get("stream"):
        parts: list[str] = []
        t_first = None
        t_gen = time.perf_counter()
        try:
            async for tok in agen:
                if t_first is None:
                    t_first = time.perf_counter()
                parts.append(tok)
        finally:
            await aclose_quiet(agen)
        dt = time.perf_counter() - t_gen
        meta = set_meta(request, timed_meta(gen.model, gen.device, gen.t_start, dt * 1000, rid))
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": gen.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)},
                         "finish_reason": "stop"}],
            "usage": _usage(gen, len(parts)),
            "tokens_per_s": round(len(parts) / dt, 2) if dt > 0 else 0.0,
            "ttft_ms": round(((t_first or t_gen) - t_gen) * 1000, 1),
            "meta": meta,
        })

    # Streaming: pull the first token before committing headers so SlotBusy -> clean 503.
    try:
        first = await anext(agen)
    except StopAsyncIteration:
        first = None
    except BaseException:
        await aclose_quiet(agen)
        raise

    def chunk(delta: dict, finish=None, **extra) -> str:
        obj = {"id": cid, "object": "chat.completion.chunk", "created": created,
               "model": gen.model,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return f"data: {json.dumps(obj)}\n\n"

    meta_holder = set_meta(request, {"model": gen.model, "device": gen.device,
                                     "queue_ms": 0.0, "infer_ms": 0.0, "request_id": rid})
    t_gen = time.perf_counter()

    async def sse():
        n = 0
        t_first = None
        try:
            yield chunk({"role": "assistant", "content": ""})
            if first is not None:
                n, t_first = 1, time.perf_counter()
                yield chunk({"content": first})
                async for tok in agen:
                    n += 1
                    yield chunk({"content": tok})
            dt = time.perf_counter() - t_gen
            meta_holder.update(timed_meta(gen.model, gen.device, gen.t_start, dt * 1000, rid))
            yield chunk({}, "stop", meta=meta_holder, usage=_usage(gen, n),
                        tokens_per_s=round(n / dt, 2) if dt > 0 else 0.0,
                        ttft_ms=round(((t_first or t_gen) - t_gen) * 1000, 1))
            yield "data: [DONE]\n\n"
        finally:
            await aclose_quiet(agen)

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.websocket("/v1/chat/stream")
async def chat_stream(ws: WebSocket):
    await ws.accept()
    services: Services = ws.app.state.services
    reader = WsReader(ws)
    try:
        while True:
            item = await reader.get()
            if item is DISCONNECT:
                return
            kind, req = item
            if kind != "json" or not isinstance(req, dict):
                await send_error(ws, "expected a JSON request {model, messages, max_tokens?}")
                continue
            turn = asyncio.create_task(_chat_turn(ws, services, req))
            nxt = asyncio.create_task(reader.get())
            await asyncio.wait({turn, nxt}, return_when=asyncio.FIRST_COMPLETED)
            if not turn.done():  # client sent something or left mid-generation
                turn.cancel()
            try:
                await turn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - reported in-band
                pass
            if nxt.done():
                if nxt.result() is DISCONNECT:
                    return
                reader.q.put_nowait(nxt.result())
            else:
                nxt.cancel()
    finally:
        await reader.close()


async def _chat_turn(ws: WebSocket, services: Services, req: dict) -> None:
    rid = new_request_id()
    gen = None
    n = 0
    t_first = None
    try:
        model = req.get("model")
        if not model:
            raise HTTPException(400, "'model' is required")
        gen = await start_generation(services, model, req.get("messages"),
                                     _int_or_none(req.get("max_tokens")))
        t_gen = time.perf_counter()
        async for tok in gen.agen:
            if t_first is None:
                t_first = time.perf_counter()
            n += 1
            await ws.send_json({"type": "token", "text": tok})
        dt = time.perf_counter() - t_gen
        meta = timed_meta(gen.model, gen.device, gen.t_start, dt * 1000, rid)
        await ws.send_json({
            "type": "done", "meta": meta, "usage": _usage(gen, n),
            "tokens_per_s": round(n / dt, 2) if dt > 0 else 0.0,
            "ttft_ms": round(((t_first or t_gen) - t_gen) * 1000, 1),
        })
        ws.scope.setdefault("state", {})["meta"] = meta  # surfaces in the inspector row
    except SlotBusy as e:
        await send_error(ws, str(e), code="slot_busy", retry_after_s=e.retry_after_s)
    except HTTPException as e:
        await send_error(ws, str(e.detail), code=f"http_{e.status_code}")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        await send_error(ws, f"{type(e).__name__}: {e}", code="internal")
    finally:
        if gen is not None:
            await aclose_quiet(gen.agen)
