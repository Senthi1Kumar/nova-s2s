"""When a finished background result is spoken: completion is not delivery.

Callers: nova_hailo.pipeline (owns one scheduler), nova_hailo.web.realtime_session
and nova_hailo.lk.nova_agent (poll it on their idle ticks).

A result is spoken only while the conversation is quiet (the user is not
talking and Nova is not mid-reply). Results that finish within one collection
window are merged into one line, so two tasks ending together do not produce
two back-to-back announcements. A delivery cut off by barge-in is re-queued and
spoken again later; the work behind it is never re-run.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nova_hailo.edge_harness.tasks import TaskRegistry


@dataclass
class _Entry:
    first_seen: float
    speak: str
    task_id: str | None = None
    research: dict[str, Any] | None = None


@dataclass
class Delivery:
    speak: str
    task_ids: list[str] = field(default_factory=list)
    research: list[dict[str, Any]] = field(default_factory=list)
    _entries: list[_Entry] = field(default_factory=list, repr=False)


def _default_announce(item: dict[str, Any]) -> str:
    from nova_hailo.web.research_speech import research_announcement

    return research_announcement(item)


class DeliveryScheduler:
    def __init__(
        self,
        registry: TaskRegistry | None,
        *,
        research_drain: Callable[[], list[dict[str, Any]]] | None = None,
        announce: Callable[[dict[str, Any]], str] | None = None,
        window_s: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._research_drain = research_drain
        self._announce = announce or _default_announce
        self._window_s = float(window_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._buffer: list[_Entry] = []
        self._seen: set[str] = set()

    def _collect(self) -> None:
        now = self._clock()
        if self._registry is not None:
            for task in self._registry.undelivered():
                if task.id in self._seen:
                    continue
                self._seen.add(task.id)
                if not task.speak:
                    self._registry.mark_delivered(task.id)
                    continue
                # The window runs from when the result became ready, not from
                # when this poll first noticed it.
                self._buffer.append(_Entry(min(now, task.updated_at), task.speak, task_id=task.id))
        if self._research_drain is not None:
            for item in self._research_drain() or []:
                speak = self._announce(item)
                if speak:
                    self._buffer.append(_Entry(now, speak, research=item))

    def poll(self, *, quiet: bool) -> Delivery | None:
        with self._lock:
            self._collect()
            if not quiet or not self._buffer:
                return None
            if self._clock() - self._buffer[0].first_seen < self._window_s:
                return None
            entries, self._buffer = self._buffer, []
        return Delivery(
            speak=" ".join(e.speak for e in entries),
            task_ids=[e.task_id for e in entries if e.task_id],
            research=[e.research for e in entries if e.research is not None],
            _entries=entries,
        )

    def delivered(self, delivery: Delivery | None) -> None:
        if delivery is None or self._registry is None:
            return
        for task_id in delivery.task_ids:
            self._registry.mark_delivered(task_id)

    def requeue(self, delivery: Delivery | None) -> None:
        """Put an interrupted delivery back; it is due again at the next quiet poll."""
        if delivery is None:
            return
        due = self._clock() - self._window_s
        with self._lock:
            again = [_Entry(due, e.speak, e.task_id, e.research) for e in delivery._entries]
            self._buffer[:0] = again

    def pending(self) -> bool:
        with self._lock:
            self._collect()
            return bool(self._buffer)
