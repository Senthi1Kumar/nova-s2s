"""Mic mute, wake and cancel for the voice session, from any display.

Callers: nova_hailo.web.app (GET /v1/session, POST /v1/session/{mute,arm,cancel}),
nova_hailo.web.realtime_session and nova_hailo.lk.door (each live session
registers a handle), the sessions themselves (a client's own mute).

Mute is the microphone only: what Nova is saying may finish. It belongs to the
cabin rather than to one connection, so it survives a client reconnecting and
is told to every session that registers; it resets when the server restarts.
Every change is published as ``nova.mic`` to /v1/observe, where the latest one
is replayed to a display that joins later.

A handle is any object with ``control_mute(muted)``, ``control_arm()``,
``control_cancel()`` and ``control_state() -> str``; each one carries the call
over to its own event loop.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from nova_hailo.web.observe import HUB


class SessionControl:
    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.muted = False
        self.source: str | None = None
        self._handles: list[Any] = []
        self._lock = threading.Lock()

    def register(self, handle: Any) -> Callable[[], None]:
        with self._lock:
            self._handles.append(handle)
            muted = self.muted
        if muted:
            handle.control_mute(True)

        def unregister() -> None:
            with self._lock:
                if handle in self._handles:
                    self._handles.remove(handle)

        return unregister

    def _latest(self) -> Any | None:
        with self._lock:
            return self._handles[-1] if self._handles else None

    def set_muted(self, muted: bool, source: str) -> bool:
        """Mute or unmute; False (and nothing published) when it is already so."""
        with self._lock:
            if bool(muted) == self.muted:
                return False
            self.muted, self.source = bool(muted), str(source)
            handles = list(self._handles)
        for handle in handles:
            handle.control_mute(self.muted)
        self.hub.publish_session_event_threadsafe(
            {"type": "nova.mic", "muted": self.muted, "source": self.source})
        return True

    def arm(self) -> dict[str, Any]:
        """Open the mic for a turn, as a wake word would."""
        if self.muted:
            return {"ok": False, "error": "mic_muted"}
        handle = self._latest()
        if handle is None:
            return {"ok": False, "error": "no_session"}
        handle.control_arm()
        return {"ok": True}

    def cancel(self) -> dict[str, Any]:
        """Stop what Nova is doing (the × button); fine when nothing is running."""
        handle = self._latest()
        if handle is not None:
            handle.control_cancel()
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        handle = self._latest()
        state = handle.control_state() if handle is not None else "IDLE"
        return {"muted": self.muted, "source": self.source, "state": state}


CONTROL = SessionControl(HUB)
