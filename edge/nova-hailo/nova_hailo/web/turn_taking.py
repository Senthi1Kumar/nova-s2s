"""Who has the turn while Nova talks. Callers: web.realtime_session, once per audio frame.

Speech over Nova lasting HOLD_S takes the turn; shorter speech is judged by its words
(classify_short). NOVA_HAILO_XVF_TURNS: "log" (default) only logs, "on" acts, "off" skips.
"""
from __future__ import annotations

import os
import re

from nova_hailo.xvf3800 import BoardState

HOLD_S = 0.5    # speech this long over Nova is the driver taking the turn
GAP_S = 0.25    # a pause this long ends an utterance
DUCK_GAIN = 0.3  # Nova's volume while a short utterance over her is being judged
DUCK_TIMEOUT_S = 3.0

# The board's speech energy overlaps between Nova and the driver, so the VAD on its
# echo-cancelled audio decides; the board only says when the canceller cannot be trusted.

_BACKCHANNEL = frozenset({
    "mm", "mhm", "hm", "hmm", "mmhm", "mmhmm", "uh", "huh", "uhhuh", "okay", "ok", "yeah", "yep", "yup",
    "yes", "right", "sure", "alright", "cool", "nice", "great", "got", "it", "i", "see", "thanks",
})
_STOP = frozenset({"stop", "wait", "no", "nope", "nova", "cancel", "pause", "quiet", "enough", "hold"})


def board_unsettled(board: BoardState | None) -> bool:
    """Canceller not converged or echo path changed; no board at all is not unsettled."""
    return board is not None and (not board.converged or board.path_change)


def turn_mode() -> str:
    mode = (os.environ.get("NOVA_HAILO_XVF_TURNS") or "log").strip().lower()
    return mode if mode in ("off", "log", "on") else "log"


def classify_short(text: str) -> str:
    """A short utterance heard over Nova: "backchannel" (she carries on), "barge" (it
    interrupts and becomes a turn), or "ignore" (nothing was said)."""
    words = re.findall(r"[a-z']+", (text or "").lower().replace("-", " "))
    if not words:
        return "ignore"
    if any(w in _STOP for w in words):
        return "barge"
    if len(words) <= 4 and all(w in _BACKCHANNEL for w in words):
        return "backchannel"
    return "barge"


class TurnTaker:
    """frame() returns "idle", "listening" (speech over Nova, not decided yet), "barge" (once,
    when the hold is reached) or "fallback" (no usable board: use the old rule)."""

    def __init__(self, hold_s: float = HOLD_S, gap_s: float = GAP_S):
        self.hold_s = hold_s
        self.gap_s = gap_s
        self._start: float | None = None
        self._last = 0.0
        self._prev: float | None = None
        self._step = 0.02
        self._decided = False
        self.last_short_s = 0.0      # length of the last utterance over Nova that ended before the hold
        self.peak_energy = 0.0       # loudest board energy in the current or last utterance

    def _reset(self) -> None:
        self._start = None
        self._decided = False

    def frame(self, now: float, *, nova_audible: bool, vad_speech: bool, board: BoardState | None) -> str:
        if self._prev is not None and 0.0 < now - self._prev < 0.2:
            self._step = now - self._prev
        self._prev = now
        if not nova_audible:
            self._reset()
            return "idle"
        if board_unsettled(board):
            self._reset()
            return "fallback"
        if vad_speech:
            if self._start is None:
                self._start, self._decided, self.peak_energy = now, False, 0.0
            self._last = now
            if board is not None:
                self.peak_energy = max(self.peak_energy, board.speech_energy)
            if not self._decided and now - self._start >= self.hold_s - 1e-9:
                self._decided = True
                return "barge"
            return "listening"
        if self._start is not None and now - self._last > self.gap_s:
            self.last_short_s = 0.0 if self._decided else self._last - self._start + self._step
            self._reset()
            return "idle"
        return "listening" if self._start is not None else "idle"


class Duck:
    """One duck-then-decide episode, from the driver's first syllable over Nova to a decision."""

    def __init__(self, hold_s: float = HOLD_S, timeout_s: float = DUCK_TIMEOUT_S):
        self.hold_s = hold_s
        self.timeout_s = timeout_s
        self.active = False
        self.t0 = 0.0
        self._prev = 0.0
        self._voiced = 0.0

    def start(self, now: float) -> bool:
        if self.active:
            return False
        self.active, self.t0, self._prev, self._voiced = True, now, now, 0.0
        return True

    def tick(self, now: float, *, vad_speech: bool, nova_audible: bool, voiced: bool | None = None) -> str:
        """voiced: this frame's VAD probability says speech. Only voiced frames count toward the
        hold; the segment state stays on through the VAD's end-of-speech hangover."""
        if not self.active:
            return "none"
        dt = min(max(now - self._prev, 0.0), 0.1)
        self._prev = now
        if not nova_audible or now - self.t0 > self.timeout_s:
            self.active = False
            return "restore"
        if voiced is None:
            spoken = now - self.t0 if vad_speech else 0.0
        else:
            self._voiced += dt if voiced else 0.0
            spoken = self._voiced
        if spoken >= self.hold_s - 1e-9:
            self.active = False
            return "barge"
        return "none"

    def short_ended(self, text: str) -> str:
        self.active = False
        return "barge" if classify_short(text) == "barge" else "restore"
