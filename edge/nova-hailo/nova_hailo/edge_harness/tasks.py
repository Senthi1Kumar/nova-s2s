"""Delegated work as task records, managed apart from the conversation.

Callers: nova_hailo.tools.oem_tools (delegate path), nova_hailo.edge_harness.delivery,
nova_hailo.pipeline.

A task moves queued -> running -> completed | failed | cancelled, or stops at
needs_input when the worker needs the user (a confirmation, a missing detail).
Interrupting speech never touches this registry: barge-in stops what Nova is
saying, not the work behind it. Only cancel() ends a task early.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

QUEUED = "queued"
RUNNING = "running"
NEEDS_INPUT = "needs_input"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
_ACTIVE = frozenset({QUEUED, RUNNING})

WORKER_FAILED_SPEAK = "I couldn't finish that."
DEFAULT_LIMITS = {"delegate": 1}


@dataclass
class Task:
    id: str
    kind: str
    objective: str
    status: str = QUEUED
    progress: str = ""
    speak: str = ""
    pending_request: Any = None
    trace: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    delivered: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "objective": self.objective,
            "status": self.status,
            "progress": self.progress,
            "speak": self.speak,
            "reason": self.reason,
        }


class TaskHandle:
    """What a worker sees of its own task."""

    def __init__(self, registry: TaskRegistry, task_id: str) -> None:
        self._registry = registry
        self.task_id = task_id

    @property
    def cancelled(self) -> bool:
        task = self._registry.get(self.task_id)
        return task is None or task.status == CANCELLED

    def progress(self, text: str) -> None:
        self._registry._update(self.task_id, progress=text)

    def needs_input(self, request: Any, speak: str, trace: dict[str, Any] | None = None) -> None:
        self._registry._finish(self.task_id, NEEDS_INPUT, speak=speak, pending_request=request,
                               trace=trace or {})

    def complete(self, speak: str, trace: dict[str, Any] | None = None) -> None:
        self._registry._finish(self.task_id, COMPLETED, speak=speak, trace=trace or {})

    def fail(self, speak: str, reason: str, trace: dict[str, Any] | None = None) -> None:
        self._registry._finish(self.task_id, FAILED, speak=speak, reason=reason,
                               trace=trace or {})


def _thread_start(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="nova-task", daemon=True).start()


class TaskRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        limits: dict[str, int] | None = None,
        ttl_s: float = 600.0,
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._clock = clock
        self._limits = dict(DEFAULT_LIMITS if limits is None else limits)
        self._ttl_s = float(ttl_s)
        self._start = start or _thread_start
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    # --- queries ---------------------------------------------------------
    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def active(self, kind: str | None = None) -> list[Task]:
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status in _ACTIVE and (kind is None or t.kind == kind)
            ]

    def undelivered(self) -> list[Task]:
        """Finished or waiting-for-the-user tasks not yet spoken, oldest first."""
        with self._lock:
            out = [
                t for t in self._tasks.values()
                if not t.delivered and (t.status in TERMINAL or t.status == NEEDS_INPUT)
            ]
        return sorted(out, key=lambda t: t.updated_at)

    # --- lifecycle -------------------------------------------------------
    def submit(
        self,
        kind: str,
        objective: str,
        worker: Callable[[TaskHandle], None],
    ) -> tuple[Task | None, str | None]:
        self.prune()
        now = self._clock()
        with self._lock:
            limit = self._limits.get(kind, 1)
            running = sum(1 for t in self._tasks.values() if t.kind == kind and t.status in _ACTIVE)
            if running >= limit:
                return None, "busy"
            task = Task(
                id=uuid.uuid4().hex[:12], kind=kind, objective=(objective or "").strip(),
                created_at=now, updated_at=now,
            )
            self._tasks[task.id] = task
        handle = TaskHandle(self, task.id)

        def run() -> None:
            if not self._update(task.id, only_from=(QUEUED,), status=RUNNING):
                return  # cancelled before it started
            try:
                worker(handle)
            except Exception as exc:  # noqa: BLE001 — a worker must never take the session down
                self._finish(task.id, FAILED, speak=WORKER_FAILED_SPEAK,
                             reason=f"worker_error:{exc}")
                return
            # A worker that returns without a result has nothing to say.
            self._finish(task.id, COMPLETED, speak="", delivered=True)

        self._start(run)
        return task, None

    def cancel(self, task_id: str) -> bool:
        return self._finish(task_id, CANCELLED, speak="", delivered=True)

    def mark_delivered(self, task_id: str) -> None:
        self._update(task_id, delivered=True)

    def prune(self) -> None:
        cutoff = self._clock() - self._ttl_s
        with self._lock:
            dead = [
                tid for tid, t in self._tasks.items()
                if t.status in TERMINAL and t.updated_at < cutoff
            ]
            for tid in dead:
                del self._tasks[tid]

    # --- internals -------------------------------------------------------
    def _update(self, task_id: str, *, only_from: tuple[str, ...] | None = None,
                **fields: Any) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or (only_from is not None and task.status not in only_from):
                return False
            for key, value in fields.items():
                setattr(task, key, value)
            task.updated_at = self._clock()
            return True

    def _finish(self, task_id: str, status: str, **fields: Any) -> bool:
        """Move to a final (or needs_input) state; a finished task never changes again."""
        return self._update(task_id, only_from=(QUEUED, RUNNING), status=status, **fields)
