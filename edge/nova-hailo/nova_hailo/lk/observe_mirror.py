"""An online (LiveKit) caller's session on /v1/observe, shaped like a /v1/realtime one.

Callers: nova_hailo.lk.door (one mirror per caller), nova_hailo.lk.nova_agent (turns).

A display watching /v1/observe should not care which door the caller used. The
LiveKit door has no realtime FSM, so the state shown is derived from what it
does have: the wake gate (idle or listening), the AgentSession's user and agent
states, and whether a pipeline turn is running. Turns get their own increasing
generation ids, so a display can tie a transcript, its trace, its card and its
reply together, and a superseded turn publishes nothing more.

Methods run on the job's event loop; the hub's *_threadsafe methods carry each
event over to the app loop that serves /v1/observe. No LiveKit imports here.
"""
from __future__ import annotations

from typing import Any

from nova_hailo.web.cards import turn_card

SESSION_ID = "livekit"


class ObserveMirror:
    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.gate = "listening"
        self.user_state = "listening"
        self.agent_state = "listening"
        self.generation_id = 0
        self._busy: int | None = None       # generation of the running pipeline turn
        self._traces: dict[int, dict[str, Any]] = {}
        self._shown: str | None = None
        self._active = False

    # ---- session -----------------------------------------------------------

    def started(self) -> None:
        self._active = True
        self.hub.session_started_threadsafe()
        self._update()

    def ended(self) -> None:
        self._active = False
        self._busy = None
        self._update()
        self.hub.session_ended_threadsafe()

    # ---- inputs ------------------------------------------------------------

    def on_gate(self, state: str) -> None:
        self.gate = state
        self._update()

    def on_user_state(self, state: str) -> None:
        self.user_state = state
        self._update()

    def on_agent_state(self, state: str) -> None:
        self.agent_state = state
        self._update()

    def event(self, event: dict[str, Any]) -> None:
        """An event the agent sent its caller (tool status, metrics, delivered results)."""
        self.hub.publish_session_event_threadsafe(event)

    # ---- turns -------------------------------------------------------------

    def turn_started(self, transcript: str) -> int:
        self.generation_id += 1
        gen = self.generation_id
        self._busy = gen
        self._traces.clear()
        self.hub.publish_session_event_threadsafe({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": transcript, "generation_id": gen,
        })
        self._update()
        return gen

    def trace(self, gen: int, payload: dict[str, Any]) -> None:
        if gen != self.generation_id:
            return
        self._traces[gen] = payload  # the turn's card reads it (route, results, media)
        self.hub.publish_session_event_threadsafe(
            {"type": "nova.agent_trace", "generation_id": gen, **payload})

    def turn_finished(self, gen: int, transcript: str, reply: str, tool_name: str | None) -> None:
        if gen != self.generation_id:
            return
        if reply:
            self.hub.publish_session_event_threadsafe(
                {"type": "response.audio_transcript.done", "transcript": reply,
                 "generation_id": gen})
            card = turn_card(transcript, reply, tool_name, trace=self._traces.get(gen))
            if card is not None:
                self.hub.publish_session_event_threadsafe(
                    {"type": "nova.card", "generation_id": gen, **card})
        self._busy = None
        self._update()

    # ---- state -------------------------------------------------------------

    def state(self) -> str:
        if not self._active:
            return "IDLE"
        if self.agent_state == "speaking":
            return "SPEAKING"
        if self._busy is not None or self.agent_state == "thinking":
            return "THINKING"
        if self.gate == "idle":
            return "IDLE"
        return "LISTENING"

    def _update(self) -> None:
        state = self.state()
        if state == self._shown:
            return
        self._shown = state
        self.hub.publish_session_event_threadsafe({
            "type": "nova.fsm",
            "fsm": {"state": state, "generation_id": self.generation_id},
            "session_id": SESSION_ID,
            "stt_engine": None,
        })
