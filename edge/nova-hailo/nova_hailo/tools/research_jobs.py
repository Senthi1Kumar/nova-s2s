"""In-memory async research job store (searching → generating → done/failed).

Callers: nova_hailo.tools.search_mcp, oem_tools deep_research, pipeline poll path.
Max one concurrent research job (Pi CPU + single GenAI permit). TTL ~5 min.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

STATUS_SEARCHING = "searching"
STATUS_GENERATING = "generating"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

DEFAULT_TTL_SEC = 300.0
POLL_INTERVAL_SEC = 0.25


@dataclass
class ResearchJob:
    job_id: str
    query: str
    status: str = STATUS_SEARCHING
    speak: str = ""
    evidence: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status,
            "query": self.query,
        }
        if self.speak:
            out["speak"] = self.speak
        if self.evidence:
            out["evidence"] = self.evidence
        if self.reason:
            out["reason"] = self.reason
        return out


class ResearchJobStore:
    """Thread-safe job dict with TTL prune and single-flight concurrency."""

    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC, max_concurrent: int = 1):
        self.ttl_sec = float(ttl_sec)
        self.max_concurrent = int(max_concurrent)
        self._lock = threading.Lock()
        self._jobs: dict[str, ResearchJob] = {}
        self._active = 0

    def prune(self) -> None:
        now = time.monotonic()
        with self._lock:
            dead = [
                jid
                for jid, job in self._jobs.items()
                if (now - job.updated_at) > self.ttl_sec
            ]
            for jid in dead:
                del self._jobs[jid]

    def try_begin(self, query: str) -> tuple[ResearchJob | None, str | None]:
        """Reserve a slot and create a searching job, or return (None, reason)."""
        self.prune()
        with self._lock:
            if self._active >= self.max_concurrent:
                return None, "research_busy"
            job = ResearchJob(job_id=uuid.uuid4().hex[:12], query=(query or "").strip())
            self._jobs[job.job_id] = job
            self._active += 1
            return job, None

    def get(self, job_id: str) -> ResearchJob | None:
        self.prune()
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        speak: str | None = None,
        evidence: str | None = None,
        reason: str | None = None,
    ) -> ResearchJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if status is not None:
                job.status = status
            if speak is not None:
                job.speak = speak
            if evidence is not None:
                job.evidence = evidence
            if reason is not None:
                job.reason = reason
            job.updated_at = time.monotonic()
            return job

    def finish(self, job_id: str) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def start_worker(
        self,
        job: ResearchJob,
        worker: Callable[[ResearchJob], None],
    ) -> None:
        def _run() -> None:
            try:
                worker(job)
            except Exception as exc:  # noqa: BLE001
                self.update(
                    job.job_id,
                    status=STATUS_FAILED,
                    speak="I couldn't finish that research.",
                    reason=f"worker_error:{exc}",
                )
            finally:
                self.finish(job.job_id)

        threading.Thread(target=_run, name=f"research-{job.job_id}", daemon=True).start()


# Process-wide store for the voice session (one Pi, one GenAI permit).
DEFAULT_STORE = ResearchJobStore()
