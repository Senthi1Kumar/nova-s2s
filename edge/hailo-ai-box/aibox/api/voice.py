"""WS /v1/voice/stream: PCM in -> transcript, tokens, done(timings), WAV out."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from fastapi import APIRouter, WebSocket

from ..media import TARGET_SR
from ..services import Services, SlotBusy
from .deps import CAP_VOICE, new_request_id, timed_meta
from .wsutil import DISCONNECT, WsReader, aclose_quiet, send_error

router = APIRouter()
DEFAULT_MODEL = "qwen3-vl"
SYSTEM_PROMPT = "You are Nova, a voice assistant. Reply in one or two short spoken sentences."
MAX_PCM_BYTES = 60 * TARGET_SR * 2
WAV_CHUNK = 32 * 1024


@router.websocket("/v1/voice/stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
    services: Services = ws.app.state.services
    reader = WsReader(ws)
    model = DEFAULT_MODEL
    pcm = bytearray()
    try:
        while True:
            item = await reader.get()
            if item is DISCONNECT:
                return
            kind, val = item
            if kind == "bytes":
                if len(pcm) + len(val) > MAX_PCM_BYTES:
                    pcm.clear()
                    await send_error(ws, "audio longer than 60s; discarded", code="too_long")
                else:
                    pcm += val
                continue
            if kind != "json" or not isinstance(val, dict):
                await send_error(ws, "expected JSON control messages or binary PCM16 frames")
                continue
            typ = val.get("type")
            if typ == "start":
                model = val.get("model") or DEFAULT_MODEL
                pcm.clear()
            elif typ == "cancel":
                # a turn in flight was already cancelled (and awaited) before this message was
                # dispatched, so nothing for it can follow this reply; outside a turn it is a no-op
                pcm.clear()
                await ws.send_json({"type": "cancelled"})
            elif typ == "stop":
                audio = bytes(pcm[: len(pcm) // 2 * 2])
                pcm.clear()
                if not audio:
                    await send_error(ws, "no audio received before stop", code="empty_audio")
                    continue
                turn = asyncio.create_task(_voice_turn(ws, services, model, audio))
                nxt = asyncio.create_task(reader.get())
                await asyncio.wait({turn, nxt}, return_when=asyncio.FIRST_COMPLETED)
                if not turn.done():  # barge-in or disconnect mid-turn: cancel generation
                    turn.cancel()
                try:
                    await turn
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - sent in-band
                    pass
                if nxt.done():
                    if nxt.result() is DISCONNECT:
                        return
                    reader.unget(nxt.result())
                else:
                    nxt.cancel()
            else:
                await send_error(ws, f"unknown message type {typ!r}")
    finally:
        await reader.close()


async def _voice_turn(ws: WebSocket, services: Services, model: str, audio: bytes) -> None:
    t_start = time.perf_counter()
    agen = None
    try:
        x = np.frombuffer(audio, "<i2").astype(np.float32) / 32768.0
        t = time.perf_counter()
        text = await services.asr.transcribe(x, None)
        asr_ms = (time.perf_counter() - t) * 1000
        await ws.send_json({"type": "transcript", "text": text})
        parts: list[str] = []
        ttft_ms = llm_ms = tts_ms = 0.0
        if text.strip():
            t = time.perf_counter()
            agen = services.chat.stream(
                model,
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
                CAP_VOICE,
            )
            async for tok in agen:
                if not parts:
                    ttft_ms = (time.perf_counter() - t) * 1000
                parts.append(tok)
                await ws.send_json({"type": "token", "text": tok})
            llm_ms = (time.perf_counter() - t) * 1000
        wav = b""
        reply = "".join(parts).strip()
        if reply:
            t = time.perf_counter()
            wav = await services.tts.synthesize(reply)
            tts_ms = (time.perf_counter() - t) * 1000
        meta = timed_meta(model, "npu", t_start, asr_ms + llm_ms + tts_ms, new_request_id())
        ws.scope.setdefault("state", {})["meta"] = meta
        await ws.send_json({
            "type": "done", "text": reply, "meta": meta,
            "timings": {"asr_ms": round(asr_ms, 1), "llm_ttft_ms": round(ttft_ms, 1),
                        "llm_ms": round(llm_ms, 1), "tts_ms": round(tts_ms, 1)},
        })
        for i in range(0, len(wav), WAV_CHUNK):
            await ws.send_bytes(wav[i : i + WAV_CHUNK])
    except SlotBusy as e:
        await send_error(ws, str(e), code="slot_busy", retry_after_s=e.retry_after_s)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        await send_error(ws, f"{type(e).__name__}: {e}", code="internal")
    finally:
        if agen is not None:
            await aclose_quiet(agen)
