"""S2S-native latency collector: per-turn + per-session percentile rollups.

Owned by the live tool-service / realtime path (not the LiteRT edge track).
Stages are milliseconds except ``decode_tok_s`` (tokens/second).
"""

from __future__ import annotations

import collections
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

# Canonical stage keys for the live cascaded loop.
S2S_STAGES: tuple[str, ...] = (
    "asr_ms",
    "driveauth_ms",
    "router_ms",
    "ttft_ms",
    "decode_tok_s",
    "tts_first_byte_ms",
    "ttfb_ms",
    "total_turn_ms",
)

DEFAULT_PERCENTILES: tuple[int, ...] = (50, 75, 90, 95, 99)
DEFAULT_MAX_TURNS = 256


@dataclass(frozen=True)
class S2STurnRecord:
    turn_id: str
    session_id: str | None
    stages: dict[str, float]
    timestamp: float


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def sanitize_stages(raw: dict[str, Any] | None) -> dict[str, float]:
    """Keep known stages with finite numeric values; drop missing/invalid."""
    if not raw:
        return {}
    out: dict[str, float] = {}
    for key in S2S_STAGES:
        if key not in raw:
            continue
        value = _as_float(raw[key])
        if value is not None:
            out[key] = value
    return out


def compute_percentiles(
    values: list[float],
    percentiles: tuple[int, ...] = DEFAULT_PERCENTILES,
) -> dict[str, float]:
    """Percentile map for a stage series. Empty input → empty dict."""
    if not values:
        return {}
    import numpy as np

    computed = np.percentile(values, list(percentiles))
    return {f"p{p}": float(v) for p, v in zip(percentiles, computed)}


def rollup_stages(
    records: list[S2STurnRecord] | collections.deque[S2STurnRecord],
    percentiles: tuple[int, ...] = DEFAULT_PERCENTILES,
) -> dict[str, dict[str, float]]:
    """Aggregate stage percentiles across records; omit stages with no samples."""
    buckets: dict[str, list[float]] = {s: [] for s in S2S_STAGES}
    for record in records:
        for stage, value in record.stages.items():
            if stage in buckets:
                buckets[stage].append(value)
    result: dict[str, dict[str, float]] = {}
    for stage, series in buckets.items():
        rolled = compute_percentiles(series, percentiles)
        if rolled:
            result[stage] = rolled
    return result


class S2SMetricsCollector:
    """Bounded ring buffer of s2s turn timings with session isolation."""

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS):
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self._max_turns = max_turns
        self._lock = threading.Lock()
        self._turns: collections.deque[S2STurnRecord] = collections.deque(
            maxlen=max_turns
        )
        self._sessions: dict[str, collections.deque[S2STurnRecord]] = {}
        # Partial stage timings measured inside tool-service endpoints,
        # merged into the next POST /metrics/s2s/turn for that session.
        self._pending: dict[str, dict[str, float]] = {}

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._sessions.clear()
            self._pending.clear()

    def stash_stage(self, session_id: str, stage: str, value_ms: float) -> None:
        """Record a server-measured stage for later merge into a turn POST."""
        if stage not in S2S_STAGES:
            return
        value = _as_float(value_ms)
        if value is None or not session_id:
            return
        with self._lock:
            bucket = self._pending.setdefault(session_id, {})
            bucket[stage] = value

    def take_pending(self, session_id: str | None) -> dict[str, float]:
        if not session_id:
            return {}
        with self._lock:
            return dict(self._pending.pop(session_id, {}))

    def record(
        self,
        stages: dict[str, Any] | None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        merge_pending: bool = True,
    ) -> S2STurnRecord:
        cleaned = sanitize_stages(stages)
        if merge_pending and session_id:
            pending = self.take_pending(session_id)
            # Explicit turn payload wins over stashed server timings.
            merged = {**pending, **cleaned}
            cleaned = sanitize_stages(merged)

        record = S2STurnRecord(
            turn_id=turn_id or str(uuid.uuid4()),
            session_id=session_id,
            stages=cleaned,
            timestamp=time.time(),
        )
        with self._lock:
            self._turns.append(record)
            if session_id:
                ring = self._sessions.get(session_id)
                if ring is None:
                    ring = collections.deque(maxlen=self._max_turns)
                    self._sessions[session_id] = ring
                ring.append(record)
        return record

    def __len__(self) -> int:
        return len(self._turns)

    def session_turn_count(self, session_id: str) -> int:
        ring = self._sessions.get(session_id)
        return 0 if ring is None else len(ring)

    def percentiles(
        self,
        percentiles: tuple[int, ...] = DEFAULT_PERCENTILES,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Return per-turn (global) and per-session rollups.

        If ``session_id`` is set, ``per_session`` contains only that session
        (empty dict when unknown). Otherwise every known session is included.
        """
        with self._lock:
            global_records = list(self._turns)
            if session_id is not None:
                ring = self._sessions.get(session_id)
                session_map = (
                    {session_id: list(ring)} if ring is not None else {session_id: []}
                )
            else:
                session_map = {sid: list(ring) for sid, ring in self._sessions.items()}

        per_session = {
            sid: rollup_stages(recs, percentiles) for sid, recs in session_map.items()
        }
        return {
            "per_turn": rollup_stages(global_records, percentiles),
            "per_session": per_session,
            "turn_count": len(global_records),
            "session_counts": {sid: len(recs) for sid, recs in session_map.items()},
        }


# Process-wide collector for the tool-service process.
s2s_metrics = S2SMetricsCollector()
