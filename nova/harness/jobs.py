"""Background job runner for Nova's agentic harness.

Sub-agent work (research, long tool chains) runs as daemon threads tracked by
``JobManager`` so the voice loop never blocks (sub-agents are
tracked background jobs with ids/status, never blocking the turn path).
``pending_announcements()`` implements the proactive "the research is back"
pattern: it hands each finished job to the caller exactly once, so the UI can
inject a spoken announcement without double-announcing.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    kind: str
    status: str  # running | done | failed
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    announced: bool = False


class JobManager:
    """Thread-safe registry of background jobs (daemon threads)."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[..., dict[str, Any]], **kwargs: Any) -> str:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, status="running")
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, fn, kwargs), daemon=True)
        thread.start()
        return job.id

    def _run(self, job: Job, fn: Callable[..., dict[str, Any]], kwargs: dict) -> None:
        try:
            result = fn(**kwargs)
            with self._lock:
                job.result = result
                job.status = "done"
        except Exception as exc:  # noqa: BLE001 - a job must never kill the process
            with self._lock:
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"
        finally:
            with self._lock:
                job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def pending_announcements(self) -> list[Job]:
        """Finished (done or failed), not-yet-announced jobs — marked announced
        on return, so each is delivered exactly once."""
        with self._lock:
            out = [
                j for j in self._jobs.values()
                if j.status in ("done", "failed") and not j.announced
            ]
            for j in out:
                j.announced = True
            return out
