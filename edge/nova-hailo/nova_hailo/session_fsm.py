"""Explicit realtime session FSM for OEM demo.

Callers: nova_hailo.web.realtime_session.
API: SessionFSM arm/begin_turn/interrupt/complete; TurnContext{turn_id,generation_id,terminal}
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionState(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"


class TurnTerminal(str, Enum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ALLOWED: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.ARMED, SessionState.LISTENING, SessionState.TRANSCRIBING},
    SessionState.ARMED: {SessionState.LISTENING, SessionState.IDLE, SessionState.TRANSCRIBING},
    SessionState.LISTENING: {
        SessionState.TRANSCRIBING,
        SessionState.INTERRUPTING,
        SessionState.IDLE,
    },
    SessionState.TRANSCRIBING: {
        SessionState.THINKING,
        SessionState.IDLE,
        SessionState.INTERRUPTING,
    },
    SessionState.THINKING: {
        SessionState.SPEAKING,
        SessionState.IDLE,
        SessionState.INTERRUPTING,
    },
    SessionState.SPEAKING: {
        SessionState.LISTENING,
        SessionState.IDLE,
        SessionState.INTERRUPTING,
        SessionState.ARMED,
        SessionState.TRANSCRIBING,
    },
    SessionState.INTERRUPTING: {
        SessionState.LISTENING,
        SessionState.IDLE,
        SessionState.ARMED,
        SessionState.TRANSCRIBING,
    },
}


@dataclass
class TurnContext:
    turn_id: str
    generation_id: int
    started_at: float = field(default_factory=time.perf_counter)
    speech_stopped_at: float | None = None
    first_pcm_sent_at: float | None = None
    first_audio_played_at: float | None = None
    barge_in_at: float | None = None
    terminal: TurnTerminal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionFSM:
    """Thread-safe session state + monotonic generation IDs."""

    def __init__(self, session_idle_sec: float = 45.0):
        self._lock = threading.RLock()
        self.state = SessionState.IDLE
        self.session_idle_sec = float(session_idle_sec)
        self._generation = 0
        self._armed_until: float | None = None
        self.current: TurnContext | None = None
        self.last_terminal: TurnTerminal | None = None

    def _transition(self, new: SessionState) -> bool:
        allowed = _ALLOWED.get(self.state, set())
        if new not in allowed and new != self.state:
            return False
        self.state = new
        return True

    def arm(self, hold_sec: float | None = None) -> None:
        with self._lock:
            self._transition(SessionState.ARMED)
            hold = self.session_idle_sec if hold_sec is None else float(hold_sec)
            self._armed_until = time.perf_counter() + hold

    def maybe_expire_arm(self) -> bool:
        with self._lock:
            if self.state == SessionState.ARMED and self._armed_until is not None:
                if time.perf_counter() >= self._armed_until:
                    self.state = SessionState.IDLE
                    self._armed_until = None
                    return True
            return False

    def begin_listen(self) -> None:
        with self._lock:
            if self.state in {SessionState.IDLE, SessionState.ARMED, SessionState.SPEAKING}:
                self._transition(SessionState.LISTENING)

    def begin_turn(self) -> TurnContext:
        with self._lock:
            self._generation += 1
            # Force TRANSCRIBING even if prior arm/listen skipped (PTT commit)
            if not self._transition(SessionState.TRANSCRIBING):
                self.state = SessionState.TRANSCRIBING
            ctx = TurnContext(
                turn_id="turn_" + uuid.uuid4().hex[:10],
                generation_id=self._generation,
                speech_stopped_at=time.perf_counter(),
            )
            self.current = ctx
            return ctx

    def set_thinking(self, generation_id: int) -> bool:
        with self._lock:
            if not self._is_current(generation_id):
                return False
            return self._transition(SessionState.THINKING)

    def set_speaking(self, generation_id: int) -> bool:
        with self._lock:
            if not self._is_current(generation_id):
                return False
            return self._transition(SessionState.SPEAKING)

    def interrupt(self, reason: str = "barge_in") -> TurnContext | None:
        with self._lock:
            self._generation += 1
            self._transition(SessionState.INTERRUPTING)
            ctx = self.current
            if ctx is not None and ctx.terminal is None:
                ctx.barge_in_at = time.perf_counter()
                ctx.terminal = TurnTerminal.CANCELLED
                ctx.metadata["interrupt_reason"] = reason
                self.last_terminal = TurnTerminal.CANCELLED
            return ctx

    def complete(
        self,
        generation_id: int,
        terminal: TurnTerminal,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if not self._is_current(generation_id):
                return False
            ctx = self.current
            if ctx is None or ctx.terminal is not None:
                return False
            ctx.terminal = terminal
            if metadata:
                ctx.metadata.update(metadata)
            self.last_terminal = terminal
            if self._armed_until and time.perf_counter() < self._armed_until:
                self.state = SessionState.LISTENING
            else:
                self.state = SessionState.IDLE
            return True

    def is_current(self, generation_id: int) -> bool:
        with self._lock:
            return self._is_current(generation_id)

    def _is_current(self, generation_id: int) -> bool:
        return (
            self.current is not None
            and self.current.generation_id == generation_id
            and self.current.terminal is None
            and generation_id == self._generation
        )

    @property
    def generation_id(self) -> int:
        with self._lock:
            return self._generation

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "generation_id": self._generation,
                "turn_id": self.current.turn_id if self.current else None,
                "terminal": self.last_terminal.value if self.last_terminal else None,
                "armed_until": self._armed_until,
            }
