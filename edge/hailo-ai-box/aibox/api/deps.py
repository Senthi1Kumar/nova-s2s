"""Shared router helpers."""

from __future__ import annotations

import time
import uuid

from fastapi import Request, WebSocket

from ..services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_services_ws(ws: WebSocket) -> Services:
    return ws.app.state.services


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def timed_meta(
    model: str, device: str, t_start: float, infer_ms: float, request_id: str | None = None
) -> dict:
    """Response `meta` block. `t_start` is a time.perf_counter() taken when the request began."""
    total_ms = (time.perf_counter() - t_start) * 1000.0
    return {
        "model": model,
        "device": device,
        "queue_ms": round(max(total_ms - infer_ms, 0.0), 2),
        "infer_ms": round(infer_ms, 2),
        "request_id": request_id or new_request_id(),
    }


def set_meta(request: Request, meta: dict) -> dict:
    """Expose meta to the inspector middleware and keep request ids aligned."""
    rid = getattr(request.state, "request_id", None)
    if rid:
        meta["request_id"] = rid
    request.state.meta = meta
    return meta

CAP_CHAT = 512
CAP_VLM = 160
CAP_VOICE = 96
