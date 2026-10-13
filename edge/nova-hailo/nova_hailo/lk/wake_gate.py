"""Wake gating for the online door: idle until "Hey Nova", then a short follow-up.

Callers: nova_hailo/lk/door.py.

Same contract as the in-car HMI: nothing is heard while idle; the wake word (or
a tap on the orb) opens the gate; after Nova answers, the user has a few seconds
of follow-up before it closes again. The window measures *silence*, so it never
closes while the user is speaking or Nova is thinking or speaking.

Event-loop only (no locking): the wake-word thread hands hits back to the loop.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Callable
from typing import Any


class WakeGate:
    def __init__(self, *, followup_s: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.followup_s = float(followup_s)
        self._clock = clock
        self.state = "idle"
        self.source = ""
        self._busy = False
        self._deadline: float | None = None
        self._forever = False

    def wake(self, source: str = "kws") -> bool:
        """Open the gate. True only on the idle -> listening edge (chime once)."""
        if self.state == "listening":
            self._restart_window()
            return False
        self.state = "listening"
        self.source = source
        self._restart_window()
        return True

    def open_forever(self) -> None:
        """No wake word available or wanted: always listening, never expires."""
        self._forever = True
        self.state = "listening"

    def busy(self, active: bool) -> None:
        """Someone is talking or Nova is working; the silence clock waits."""
        self._busy = active
        if not active and self.state == "listening":
            self._restart_window()

    def close(self) -> bool:
        """Close an open gate now. True on that edge; never closes an always-open one."""
        if self._forever or self.state != "listening":
            return False
        self.state = "idle"
        self._deadline = None
        return True

    def expire(self) -> bool:
        """Close the gate if the follow-up window ran out. True on that edge."""
        if self._forever or self.state != "listening" or self._busy:
            return False
        if self._deadline is not None and self._clock() >= self._deadline:
            self.state = "idle"
            self._deadline = None
            return True
        return False

    def _restart_window(self) -> None:
        self._deadline = self._clock() + self.followup_s


class WakeController:
    """Drives a WakeGate from the room and drives the session from the gate.

    In:  the caller's mic frames (wake word), session user/agent states, taps.
    Out: speech input paused/resumed on the AgentSession, and nova.state
         messages so the page can chime and show Idle/Listening.
    With no detector the gate is simply open: today's always-listening mode.
    """

    def __init__(
        self,
        *,
        gate: WakeGate,
        detector: Any,
        set_audio_enabled: Callable[[bool], None],
        send_state: Callable[..., None],
    ) -> None:
        self.gate = gate
        self._detector = detector
        self._set_audio = set_audio_enabled
        self._send_state = send_state
        self._user_busy = False
        self._agent_busy = False
        self._muted = False

    def set_muted(self, muted: bool) -> None:
        """The cabin mic is off: speech input pauses whatever the gate says, and a
        wake (word or tap) is ignored until it is back on."""
        self._muted = bool(muted)
        self._set_audio(not self._muted and self.gate.state == "listening")

    def close(self) -> None:
        """× on a display: shut an open gate (an always-listening room stays open)."""
        if self.gate.close():
            self._set_audio(False)
            self._send_state("idle")

    def start(self) -> None:
        if self._detector is None:
            self.gate.open_forever()
            self._send_state("listening", wake="session")
            return
        self._set_audio(False)
        self._send_state("idle")

    def wake(self, source: str) -> None:
        if self._muted:
            return  # mute beats wake
        if self.gate.wake(source):
            self._set_audio(True)
            self._send_state("listening", wake=source)

    def on_user_state(self, state: str) -> None:
        self._user_busy = state == "speaking"
        self.gate.busy(self._user_busy or self._agent_busy)

    def on_agent_state(self, state: str) -> None:
        self._agent_busy = state in ("thinking", "speaking")
        self.gate.busy(self._user_busy or self._agent_busy)

    def check_expiry(self) -> bool:
        if self.gate.expire():
            self._set_audio(False)
            self._send_state("idle")
            return True
        return False

    async def run_kws(self, frames: AsyncIterable[Any]) -> None:
        """Feed the caller's 16 kHz mic frames to the wake word while idle."""
        async for frame in frames:
            if self._detector is None or self.gate.state != "idle" or self._muted:
                continue  # the detector rests while the gate is open or the mic is off
            hit = await asyncio.to_thread(self._detector.process, bytes(frame.data))
            if hit:
                self.wake("kws")

    async def run_expiry(self, tick_s: float = 0.25) -> None:
        while True:
            await asyncio.sleep(tick_s)
            self.check_expiry()
