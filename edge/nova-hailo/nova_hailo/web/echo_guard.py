"""Is a transcript Nova hearing herself? Checked against what she actually said lately.

Callers: nova_hailo.web.realtime_session (records every clause it speaks, checks
every transcript before it becomes a turn).

The speaker feeds the microphone. When the mic gate or the echo canceller lets
some of Nova's voice through, speech recognition turns it into a "user" turn
that fires tools, re-asks old context and repeats itself. Comparing against
the last stored reply missed most of it: interrupted replies never reach the
history, and an echo often spans two clauses. So every spoken clause is kept
(interrupted ones too) for a short window, and a transcript whose word pairs
mostly come from that recent speech is treated as echo.
"""
from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable


def fold_words(text: str) -> list[str]:
    t = (text or "").lower().replace("'", "")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\d+", "#", t)
    return t.split()


def _bigrams(words: list[str]) -> set[tuple[str, str]]:
    return set(zip(words, words[1:]))


class EchoGuard:
    def __init__(
        self,
        *,
        keep: int = 8,
        window_s: float = 20.0,
        threshold: float = 0.6,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spoken: deque[tuple[float, str]] = deque(maxlen=int(keep))
        self.window_s = float(window_s)
        self.threshold = float(threshold)
        self._clock = clock

    def spoken(self, clause: str) -> None:
        """A clause Nova is saying, whether or not it is heard to the end."""
        if (clause or "").strip():
            self._spoken.append((self._clock(), clause))

    def recent_text(self) -> str:
        now = self._clock()
        return " ".join(c for t, c in self._spoken if now - t <= self.window_s)

    def is_echo(self, transcript: str) -> bool:
        words = fold_words(transcript)
        if len(words) < 2:
            return False  # "yes", "stop": too short to tell, and the most important to keep
        pairs = _bigrams(words)
        heard = _bigrams(fold_words(self.recent_text()))
        if not heard:
            return False
        return len(pairs & heard) / len(pairs) >= self.threshold
