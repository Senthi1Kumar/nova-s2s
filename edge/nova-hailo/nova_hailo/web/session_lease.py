"""One conversation at a time across every front door onto the pipeline.

Callers: nova_hailo/web/app.py (/v1/realtime) and the LiveKit job.

The Pi holds a single NovaPipeline (one Hailo device, one TTS queue, one
history), so the LAN socket and an online LiveKit room must not run turns at
once. Policy is first come, first served: the loser gets None and tells its
client Nova is busy.
"""
from __future__ import annotations

import threading


class Lease:
    """A held claim. release() is idempotent and only ever frees its own claim,
    so a late second release from an old session cannot evict the next one."""

    def __init__(self, parent: SessionLease, owner: str) -> None:
        self._parent = parent
        self.owner = owner

    def release(self) -> None:
        self._parent._release(self)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class SessionLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Lease | None = None

    def try_acquire(self, owner: str) -> Lease | None:
        with self._lock:
            if self._held is not None:
                return None
            self._held = Lease(self, owner)
            return self._held

    @property
    def holder(self) -> str | None:
        with self._lock:
            return self._held.owner if self._held is not None else None

    def _release(self, lease: Lease) -> None:
        with self._lock:
            if self._held is lease:
                self._held = None
