"""Generation invalidation for barge-in / stop — no PCM after cancel."""
from __future__ import annotations


class GenerationGuard:
    """Host-side generation id. Interrupt marks current work stale."""

    def __init__(self) -> None:
        self._gen = 0
        self._cancelled = False

    @property
    def generation_id(self) -> int:
        return self._gen

    def begin(self) -> int:
        self._gen += 1
        self._cancelled = False
        return self._gen

    def invalidate(self, reason: str = "barge_in") -> int:
        """Cancel in-flight playback/generation; returns new (stale-for-old) id."""
        self._cancelled = True
        self._gen += 1
        self._last_reason = reason
        return self._gen

    def is_live(self, generation_id: int) -> bool:
        return (not self._cancelled) and generation_id == self._gen

    def should_enqueue_pcm(self, generation_id: int) -> bool:
        """False after interrupt — callers must not enqueue more TTS PCM."""
        return self.is_live(generation_id)
