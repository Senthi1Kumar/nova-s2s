"""Versioned WS /v1/voice/stream backed by one Pipecat session per connection."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from ..services import Services
from ..voice.pipeline import run_voice_session

router = APIRouter()


@router.websocket("/v1/voice/stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
    services: Services = ws.app.state.services
    await run_voice_session(ws, services)
