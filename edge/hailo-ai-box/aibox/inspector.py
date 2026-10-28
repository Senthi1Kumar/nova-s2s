"""Request inspector: bounded ring buffer, live subscribers, client table."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

RING_SIZE = 2000
SUBSCRIBER_QUEUE = 256


class _Subscription:
    """Async iterator registered the moment it is created (no first-next race)."""

    def __init__(self, owner: Inspector) -> None:
        self._owner = owner
        self._q: asyncio.Queue[dict] = asyncio.Queue(SUBSCRIBER_QUEUE)
        owner._subs.append(self)

    def _push(self, entry: dict) -> None:
        if self._q.full():
            self._q.get_nowait()  # slow consumer: drop oldest
        self._q.put_nowait(entry)

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> dict:
        return await self._q.get()

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._owner._subs.remove(self)
        except ValueError:
            pass

    def __del__(self) -> None:
        self.close()


class Inspector:
    def __init__(self, size: int = RING_SIZE) -> None:
        self._ring: deque[dict] = deque(maxlen=size)
        self._subs: list[_Subscription] = []
        self._clients: dict[str, dict] = {}

    def record(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("t", time.time())
        self._ring.append(entry)
        for s in list(self._subs):
            s._push(entry)

    def event(self, event: str, **detail: Any) -> None:
        # trailing underscores let callers pass Python keywords (from_ -> "from")
        detail = {k.rstrip("_") if k.endswith("_") else k: v for k, v in detail.items()}
        self.record({"t": time.time(), "kind": "event", "event": event, "detail": detail})

    def recent(self, n: int = 200) -> list[dict]:
        return list(self._ring)[-n:] if n > 0 else []

    def subscribe(self) -> _Subscription:
        """Use as `async for entry in inspector.subscribe()`; call close()/aclose() when done."""
        return _Subscription(self)

    def clients(self) -> list[dict]:
        return [dict(c) for c in self._clients.values()]

    def touch_client(self, client_id: str, addr: str, user_agent: str) -> None:
        c = self._clients.get(client_id)
        if c is None:
            c = {"id": client_id, "addr": addr, "user_agent": user_agent, "last_seen": 0.0,
                 "requests": 0}
            self._clients[client_id] = c
        c.update(addr=addr, user_agent=user_agent, last_seen=time.time())
        c["requests"] += 1
