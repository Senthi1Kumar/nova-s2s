"""GenAI slot broker: the chip has ONE KV-cache slot, shared by the in-process VLM and hailo-ollama."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from ..services import SlotBusy
from .registry import Emitter

MAX_WAITERS = 8
SWAP_RETRY_AFTER_S = 5.0


async def _call(fn: Callable, *args: Any) -> Any:
    """Run a sync callable on a thread, or await an async one."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    res = await asyncio.to_thread(fn, *args)
    return await res if inspect.isawaitable(res) else res


class GenAISlot:
    """`loaders[owner_key]()` returns a handle; the slot calls `handle.unload()` on eviction.

    Owner keys look like `vlm:qwen3-vl` or `ollama:qwen3:1.7b`. Requests are served FIFO, one
    generation at a time; at most `max_waiters` may queue behind the holder, the rest get SlotBusy.
    """

    def __init__(self, inspector: Any, loaders: dict[str, Callable] | Callable[[str], Callable],
                 max_waiters: int = MAX_WAITERS) -> None:
        self.emit = inspector if isinstance(inspector, Emitter) else Emitter(inspector)
        self._loaders = loaders
        self._max_waiters = max_waiters
        self._lock = asyncio.Lock()
        self._waiting = 0
        self._owner: str | None = None
        self._handle: Any = None
        self._state = "idle"
        self.last_swap_ms: dict[str, float] = {}

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def state(self) -> str:
        return self._state

    def waiting(self) -> int:
        return self._waiting

    def _loader_for(self, key: str) -> Callable:
        if callable(self._loaders):
            return self._loaders(key)
        try:
            return self._loaders[key]
        except KeyError:
            raise ValueError(f"no GenAI loader for {key!r}") from None

    async def _swap(self, to: str) -> None:
        prev = self._owner
        t0 = time.monotonic()
        self._state = "swapping"
        loader = self._loader_for(to)
        try:
            if self._handle is not None:
                handle, self._handle, self._owner = self._handle, None, None
                unload = getattr(handle, "unload", None)
                if unload is not None:
                    await _call(unload)
            self._handle = await _call(loader)
            self._owner = to
        except BaseException as exc:
            self._owner = None
            self._handle = None
            self.emit("genai_swap_failed", from_=prev, to=to, error=f"{type(exc).__name__}: {exc}")
            raise
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        self.last_swap_ms[f"{prev}->{to}"] = ms
        self.emit("genai_swap", from_=prev, to=to, ms=ms)

    @asynccontextmanager
    async def acquire(self, model: str) -> AsyncIterator[Any]:
        if self._lock.locked() and self._waiting >= self._max_waiters:
            raise SlotBusy(SWAP_RETRY_AFTER_S)
        self._waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiting -= 1
        try:
            if self._owner != model:
                task = asyncio.ensure_future(self._swap(model))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # let the HailoRT load/unload finish rather than abandon it half-way
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            self._state = "generating"
            yield self._handle
        finally:
            self._state = "idle"
            self._lock.release()

    async def release_all(self) -> None:
        async with self._lock:
            if self._handle is not None:
                handle, self._handle, self._owner = self._handle, None, None
                unload = getattr(handle, "unload", None)
                if unload is not None:
                    await _call(unload)
