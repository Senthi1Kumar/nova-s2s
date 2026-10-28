"""ASR + TTS: /v1/audio/transcriptions, /v1/audio/speech."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ..media import TARGET_SR, MediaError, decode_audio
from ..services import Services
from .deps import get_services, set_meta, timed_meta

router = APIRouter()
MAX_UPLOAD = 25 * 1024 * 1024
MAX_TTS_CHARS = 2000


async def read_upload(f: UploadFile, what: str) -> bytes:
    data = await f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{what} larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    return data


@router.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-base"),
    language: str | None = Form(None),
    services: Services = Depends(get_services),
):
    if model != "whisper-base":
        raise HTTPException(404, f"model '{model}' not found")
    data = await read_upload(file, "audio")
    t0 = time.perf_counter()
    try:
        pcm = await asyncio.to_thread(decode_audio, data)
    except MediaError as e:
        raise HTTPException(e.status, e.message) from e
    t1 = time.perf_counter()
    text = await services.asr.transcribe(pcm, language)
    infer_ms = (time.perf_counter() - t1) * 1000
    meta = set_meta(request, timed_meta(model, "npu", t0, infer_ms))
    return {"text": text, "meta": meta, "audio_s": round(pcm.size / TARGET_SR, 3)}


@router.post("/v1/audio/speech")
async def speech(request: Request, services: Services = Depends(get_services)):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "request body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    text = str(body.get("input") or "").strip()
    if not text:
        raise HTTPException(400, "'input' must be non-empty text")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(413, f"'input' longer than {MAX_TTS_CHARS} characters")
    if (body.get("response_format") or "wav") != "wav":
        raise HTTPException(400, "only response_format 'wav' is supported")
    t0 = time.perf_counter()
    wav = await services.tts.synthesize(text, body.get("voice"))
    ms = (time.perf_counter() - t0) * 1000
    set_meta(request, timed_meta("piper-amy", "cpu", t0, ms))
    return Response(wav, media_type="audio/wav", headers={"X-Infer-Ms": f"{ms:.1f}"})
