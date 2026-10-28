"""WebSocket helpers: a reader task that detects disconnects, and shielded generator close."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import anyio
from fastapi import WebSocket

DISCONNECT = object()


class WsReader:
    """Reads messages into a queue so the handler can watch for disconnects while it works.

    Items are ("json", obj) | ("bytes", b) | ("bad", text) | DISCONNECT.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                m = await self.ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    self.q.put_nowait(("bytes", m["bytes"]))
                elif m.get("text") is not None:
                    try:
                        self.q.put_nowait(("json", json.loads(m["text"])))
                    except ValueError:
                        self.q.put_nowait(("bad", m["text"]))
        except Exception:  # noqa: BLE001 - treat any socket failure as a disconnect
            pass
        self.q.put_nowait(DISCONNECT)

    async def get(self) -> Any:
        return await self.q.get()

    def unget(self, item: Any) -> None:
        """Put an already-read item back at the head so message order is preserved."""
        self.q._queue.appendleft(item)  # type: ignore[attr-defined]  # asyncio.Queue has no putleft

    async def close(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


async def aclose_quiet(agen: Any) -> None:
    """Close an async generator even when the surrounding task is being cancelled."""
    close = getattr(agen, "aclose", None)
    if close is None:
        return
    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        await close()


async def send_error(ws: WebSocket, message: str, **extra: Any) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "error", "error": message, **extra})
