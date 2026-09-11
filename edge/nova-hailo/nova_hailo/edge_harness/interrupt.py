"""Generation invalidation for barge-in / stop — no PCM after cancel."""
from __future__ import annotations


class GenerationGuard:
    """Host-side generation id. Interrupt marks current work stale.

    After ``invalidate()``, a discarding window drops output from the old
    generation until ``begin()`` starts the next turn (same idea as s2s
    CancelScope, without importing that runtime).
    """

    def __init__(self) -> None:
        self._gen = 0
        self._cancelled = False
        self._discarding = False
        self._discarded_generation: int | None = None
        self._last_reason = ""

    @property
    def generation_id(self) -> int:
        return self._gen

    @property
    def discarding(self) -> bool:
        return self._discarding

    @property
    def last_reason(self) -> str:
        return self._last_reason

    def begin(self) -> int:
        self._gen += 1
        self._cancelled = False
        self._discarding = False
        self._discarded_generation = None
        return self._gen

    def invalidate(self, reason: str = "barge_in") -> int:
        """Cancel in-flight playback/generation; returns new (stale-for-old) id."""
        self._discarded_generation = self._gen
        self._cancelled = True
        self._discarding = True
        self._last_reason = reason
        self._gen += 1
        return self._gen

    def is_live(self, generation_id: int) -> bool:
        return (not self._cancelled) and generation_id == self._gen

    def should_enqueue_pcm(self, generation_id: int) -> bool:
        """False after interrupt — callers must not enqueue more TTS PCM."""
        if self._discarding and generation_id != self._gen:
            return False
        return self.is_live(generation_id)
