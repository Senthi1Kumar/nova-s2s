"""nova.* control messages over the LiveKit data channel.

The browser UI speaks the same JSON it would speak on /v1/realtime
(nova.settings.get/set, nova.google.*; out: nova.settings, nova.llm_status,
nova.turn_metrics, nova.tool_status), on one reliable data topic. Transcripts are
not carried here: LiveKit already publishes user and agent transcriptions into
the room. Voice-frontend settings (noise gate, DTLN) act on the Pi's own mic
path, which an online caller does not use, so they are not offered.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from nova_hailo.web import settings_ops

logger = logging.getLogger("nova.lk")

TOPIC = "nova.control"
# Control messages are small; anything larger is a bug or abuse, not a setting.
_MAX_MSG_BYTES = 16 * 1024


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


class NovaControl:
    def __init__(self, room: Any, pipeline: Any) -> None:
        self._room = room
        self._pipeline = pipeline
        self._tasks: set[asyncio.Task] = set()
        # Set by the door when wake gating is on: a tap on the orb is a wake
        # event, and nova.settings reports the gate so a late page shows it.
        self.on_wake: Callable[[], None] | None = None
        self.state_extra: Callable[[], dict[str, Any]] | None = None
        # Called after the browser switched the LLM (the door tells /v1/observe displays).
        self.on_llm_changed: Callable[[], None] | None = None
        # The browser's mic button: nova.mic.set {"muted": bool} (cabin-wide mute).
        self.on_mic_set: Callable[[bool], None] | None = None

    def attach(self) -> None:
        self._room.on("data_received", self._on_data)

    def send(self, msg: dict[str, Any]) -> None:
        """Publish one message. Event-loop thread only; never raises."""
        self._spawn(self._publish(msg))

    async def drain(self) -> None:
        """Wait for every in-flight handler and publish (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---- internals ---------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(self, msg: dict[str, Any]) -> None:
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg), reliable=True, topic=TOPIC
            )
        except Exception:
            logger.warning("control publish failed for %s", msg.get("type"), exc_info=True)

    def _on_data(self, packet: Any) -> None:
        if getattr(packet, "topic", "") != TOPIC:
            return
        data = packet.data
        if len(data) > _MAX_MSG_BYTES:
            self.send(_error("message_too_large", f"limit is {_MAX_MSG_BYTES} bytes"))
            return
        try:
            msg = json.loads(data)
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            self.send(_error("invalid_json", str(exc)))
            return
        self._spawn(self._handle(msg))

    def _settings(self, llm: dict[str, Any] | None = None) -> dict[str, Any]:
        snap = dict(llm if llm is not None else settings_ops.llm_snapshot(self._pipeline))
        snap.update(settings_ops.integrations_snapshot())
        snap["transport"] = "livekit"
        if self.state_extra is not None:
            snap.update(self.state_extra())
        return {"type": "nova.settings", **snap}

    async def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "nova.wake":
            if self.on_wake is not None:
                self.on_wake()
            return
        if mtype == "nova.mic.set":
            muted = msg.get("muted")
            if not isinstance(muted, bool):
                await self._publish(_error("bad_muted", "muted must be true or false"))
            elif self.on_mic_set is not None:
                self.on_mic_set(muted)
            return
        if mtype == "nova.settings.get":
            await self._publish(self._settings())
        elif mtype == "nova.settings.set":
            payload = settings_ops.settings_body(msg)
            if not settings_ops.changes_llm(payload):
                await self._publish(self._settings())
                return
            await self._publish({"type": "nova.llm_status", "status": "loading"})
            snap, status = await settings_ops.apply_llm_settings(self._pipeline, payload)
            if snap is not None:
                await self._publish(self._settings(snap))
                if self.on_llm_changed is not None:
                    self.on_llm_changed()
            await self._publish(status)
        elif mtype == "nova.google.connect":
            await self._publish(await asyncio.to_thread(settings_ops.google_connect_payload))
            await self._publish(self._settings())
        elif mtype == "nova.google.disconnect":
            await self._publish(settings_ops.google_disconnect_payload())
            await self._publish(self._settings())
        else:
            await self._publish(_error("unknown_message", str(mtype)))
