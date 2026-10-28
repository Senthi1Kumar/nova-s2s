"""Model residency registry: state machine, load timing and rolling per-model stats."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..inspector import Inspector

STATES = ("unloaded", "loading", "resident", "evicting", "failed")
WINDOW_S = 60.0


class Emitter:
    """Inspector events from any thread (asyncio queues are not thread-safe)."""

    def __init__(self, inspector: Inspector) -> None:
        self.inspector = inspector
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def unbind(self) -> None:
        self._loop = None

    def __call__(self, event: str, **detail: Any) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and self._loop.is_closed():
            self._loop = None
        if self._loop is None and running is not None:
            self._loop = running  # first event inside the server loop pins it
        loop = self._loop
        if loop is None:
            self.inspector.event(event, **detail)
            return
        if running is loop:
            self.inspector.event(event, **detail)
        else:
            loop.call_soon_threadsafe(lambda: self.inspector.event(event, **detail))


@dataclass
class ModelSpec:
    id: str
    modality: str  # chat|vlm|asr|tts|vision
    runtime: str  # hailo-ollama|hailort-genai|hailort|piper
    task: str | None = None
    device: str = "npu"
    size_mb: float | None = None
    hef: str | None = None


@dataclass
class _Entry:
    spec: ModelSpec
    state: str = "unloaded"
    error: str | None = None
    load_s: float | None = None
    last_used: float | None = None
    calls: deque = field(default_factory=deque)  # (t, ms)


class Registry:
    def __init__(self, inspector: Inspector | Emitter) -> None:
        self.emit = inspector if isinstance(inspector, Emitter) else Emitter(inspector)
        self._m: dict[str, _Entry] = {}
        self._lock = threading.RLock()

    def register(self, spec: ModelSpec) -> None:
        with self._lock:
            self._m.setdefault(spec.id, _Entry(spec))

    def ids(self) -> list[str]:
        return list(self._m)

    def state(self, model_id: str) -> str:
        return self._m[model_id].state

    def set_state(self, model_id: str, state: str, error: str | None = None,
                  load_s: float | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"bad state {state!r}")
        with self._lock:
            e = self._m[model_id]
            prev = e.state
            e.state = state
            e.error = error if state == "failed" else None
            if load_s is not None:
                e.load_s = round(load_s, 2)
        self.emit("model_state", model=model_id, state=state, prev=prev,
                  error=error, load_s=None if load_s is None else round(load_s, 2))

    def load(self, model_id: str, loader: Callable[[], Any]) -> Any | None:
        """Run `loader` and move the model unloaded -> loading -> resident|failed. Never raises."""
        self.set_state(model_id, "loading")
        t0 = time.monotonic()
        try:
            result = loader()
        except Exception as exc:  # noqa: BLE001 - a model failing to load must not stop the box
            self.set_state(model_id, "failed", error=f"{type(exc).__name__}: {exc}")
            return None
        self.set_state(model_id, "resident", load_s=time.monotonic() - t0)
        return result

    def record_call(self, model_id: str, ms: float) -> None:
        now = time.time()
        with self._lock:
            e = self._m.get(model_id)
            if e is None:
                return
            e.last_used = now
            e.calls.append((now, ms))
            self._trim(e, now)

    @staticmethod
    def _trim(e: _Entry, now: float) -> None:
        while e.calls and e.calls[0][0] < now - WINDOW_S:
            e.calls.popleft()

    def items(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": e.spec.id, "object": "model", "owned_by": "hailo",
                    "modality": e.spec.modality, "task": e.spec.task,
                    "runtime": e.spec.runtime, "resident": e.state == "resident",
                    "device": e.spec.device, "state": e.state, "error": e.error,
                    "load_s": e.load_s, "size_mb": e.spec.size_mb, "last_used": e.last_used,
                }
                for e in self._m.values()
            ]

    def stats(self) -> list[dict]:
        now = time.time()
        out = []
        with self._lock:
            for e in self._m.values():
                self._trim(e, now)
                ms = sorted(c[1] for c in e.calls)
                n = len(ms)
                span = min(WINDOW_S, now - e.calls[0][0]) if n else 0.0
                out.append({
                    "id": e.spec.id, "state": e.state,
                    "fps": round(n / max(span, 1.0), 2) if n else 0.0,
                    "p50_ms": round(ms[n // 2], 1) if n else 0.0,
                    "p95_ms": round(ms[min(n - 1, int(n * 0.95))], 1) if n else 0.0,
                    "requests": n,
                })
        return out
