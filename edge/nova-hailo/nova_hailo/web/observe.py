"""Read-only event mirror for displays: the /v1/observe WebSocket.

Callers: nova_hailo.web.app (endpoint, vehicle feed), nova_hailo.web.realtime_session
(publishes the events it sends to its own client).

/v1/realtime admits one client, which owns the microphone and the speaker. A
display (the LVGL cabin UI, a dashboard) only needs to see what is happening:
the Nova state, transcripts, tool and trace events, and vehicle state. Each
observer gets a bounded queue; when it falls behind, the oldest events are
dropped, so a slow display can never stall a live voice session. Audio is
never mirrored.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

# Transcript and turn events a display needs besides the nova.* family.
PROTOCOL = "nova-observe/1"
# Kept so an observer that joins mid-session can draw the current state at once.
_REPLAYED = ("nova.fsm", "nova.nav", "nova.nav.progress", "nova.agent_trace", "nova.settings", "nova.mic")

_SESSION_TYPES = frozenset({
    "session.armed",
    "wake.detected",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.input_audio_transcription.completed",
    "response.audio_transcript.delta",
    "response.audio_transcript.done",
    "response.done",
})


def _mirrored(event_type: str) -> bool:
    if event_type == "nova.vehicle":
        return False  # vehicle state comes from the app-level feed, once
    return event_type.startswith("nova.") or event_type in _SESSION_TYPES


class ObserverHub:
    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = int(maxsize)
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last: dict[str, dict[str, Any]] = {}
        self.session_active = False
        self.vehicle_seq = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def observer_count(self) -> int:
        return len(self._queues)

    def _put(self, event: dict[str, Any] | None) -> None:
        for q in list(self._queues):
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest; never block the producer
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    def publish_session_event(self, event: dict[str, Any]) -> None:
        """An event the realtime session sent its client (loop thread)."""
        event_type = str(event.get("type") or "")
        if not _mirrored(event_type):
            return
        if event_type in _REPLAYED:
            self._last[event_type] = event
        if self._queues:
            self._put(event)

    def publish_vehicle(self, event: dict[str, Any]) -> None:
        """App-level state (vehicle deltas, nova.nav); runs on the loop thread."""
        if event.get("type") in _REPLAYED:
            self._last[str(event["type"])] = event
        self.vehicle_seq = max(self.vehicle_seq, int(event.get("seq") or 0))
        if self._queues:
            self._put(event)

    def session_started(self) -> None:
        self.session_active = True
        self._put({"type": "observe.session", "active": True})

    def session_ended(self) -> None:
        self.session_active = False
        self._put({"type": "observe.session", "active": False})

    def replay(self) -> list[dict[str, Any]]:
        """The latest state events, for an observer joining now."""
        return [self._last[t] for t in _REPLAYED if t in self._last]

    def publish_vehicle_threadsafe(self, event: dict[str, Any]) -> None:
        """VssStore listeners run on whichever thread wrote the store."""
        self._call_soon(self.publish_vehicle, event)

    # The LiveKit door runs each caller on its own job thread and event loop.
    def publish_session_event_threadsafe(self, event: dict[str, Any]) -> None:
        self._call_soon(self.publish_session_event, event)

    def session_started_threadsafe(self) -> None:
        self._call_soon(self.session_started)

    def session_ended_threadsafe(self) -> None:
        self._call_soon(self.session_ended)

    def _call_soon(self, fn: Callable[..., None], *args: Any) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(fn, *args)

    def close(self) -> None:
        """End every observer stream (server shutdown)."""
        self._put(None)


HUB = ObserverHub()


async def serve_observer(
    ws: Any,
    hub: ObserverHub,
    *,
    snapshot: Callable[[int], dict[str, Any] | None],
) -> None:
    """Stream mirrored events to one observer until it disconnects or the hub closes.

    On connect: a hello, the latest ``nova.fsm``, a full vehicle snapshot tagged
    with the current seq, and the latest ``nova.agent_trace``; then live events.
    Subscribes before taking the snapshot, so no change can fall between the
    snapshot and the first live event (a change may arrive twice; it is idempotent).
    """
    q = hub.subscribe()
    try:
        await ws.send_text(json.dumps({
            "type": "observe.hello", "protocol": PROTOCOL, "session_active": hub.session_active,
        }))
        replay = hub.replay()
        for event in (e for e in replay if e.get("type") == "nova.fsm"):
            await ws.send_text(json.dumps(event))
        snap = snapshot(hub.vehicle_seq)
        if snap is not None:
            await ws.send_text(json.dumps(snap))
        for event in (e for e in replay if e.get("type") != "nova.fsm"):
            await ws.send_text(json.dumps(event))
        while True:
            event = await q.get()
            if event is None:
                return
            await ws.send_text(json.dumps(event))
    except Exception:  # noqa: BLE001 — a closed display socket ends only this stream
        return
    finally:
        hub.unsubscribe(q)
