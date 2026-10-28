"""Face tracking for the Face ID overlay: driver face box + SCRFD's 5 landmarks, EMA-smoothed."""

from __future__ import annotations

import numpy as np

LOST_AFTER = 4          # frames without a face before the track is dropped
JUMP_FRAC = 0.6         # a box centre moving more than this many box widths is a new face: no blending


class FaceSmoother:
    """Per-stream exponential moving average of one face (bbox + 5 landmarks)."""

    def __init__(self, alpha: float = 0.55) -> None:
        self.alpha = alpha
        self._state: np.ndarray | None = None   # [x1, y1, x2, y2, lx0, ly0, ..., lx4, ly4]
        self._missed = 0

    def reset(self) -> None:
        self._state = None
        self._missed = 0

    def update(self, face: dict | None) -> dict | None:
        if face is None:
            self._missed += 1
            if self._missed >= LOST_AFTER:
                self._state = None
            return self._emit(None) if self._state is not None else None
        self._missed = 0
        raw = np.asarray(list(face["bbox"]) + [c for p in face["landmarks"] for c in p],
                         dtype=np.float64)
        if self._state is not None:
            width = max(self._state[2] - self._state[0], 1.0)
            moved = abs((raw[0] + raw[2]) / 2 - (self._state[0] + self._state[2]) / 2)
            if moved > JUMP_FRAC * width:
                self._state = None
        self._state = raw if self._state is None else self.alpha * raw + (1 - self.alpha) * self._state
        return self._emit(face)

    def _emit(self, face: dict | None) -> dict:
        s = self._state
        return {
            "class": "face",
            "confidence": float(face["confidence"]) if face else 0.0,
            "bbox": [round(float(v), 1) for v in s[:4]],
            "landmarks": [[round(float(s[4 + 2 * i]), 1), round(float(s[5 + 2 * i]), 1)]
                          for i in range(5)],
            "held": face is None,
        }
