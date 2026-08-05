"""Session rollups matching hailo_ollama_bench MetricsTracker summary keys."""
from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Any

from nova_hailo.bench.contracts import summarize_latency


def _r(v: float | None, nd: int = 2) -> float | None:
    if v is None:
        return None
    return round(float(v), nd)


class BenchTracker:
    """Aggregate turn + LLM metrics for the web bench panel."""

    def __init__(self, model_params_b: float = 1.5):
        self.model_params_b = model_params_b
        self.turns: list[dict[str, Any]] = []
        self._rolling_tok_s: deque[float] = deque(maxlen=50)
        self._rolling_power_w: deque[float] = deque(maxlen=100)
        self._lock_ts: list[tuple[float, float]] = []

    def record_power(self, watts: float):
        if watts is None:
            return
        self._rolling_power_w.append(float(watts))

    def add_turn(self, turn: dict[str, Any]):
        self.turns.append(turn)
        tok = turn.get("decode_tok_s") or turn.get("tok_s")
        if tok:
            self._rolling_tok_s.append(float(tok))
        llm = turn.get("llm") or {}
        total_ms = llm.get("llm_total_ms") or turn.get("llm_ms")
        if total_ms:
            end = time.time()
            start = end - (float(total_ms) / 1000.0)
            self._lock_ts.append((start, end))

    @property
    def total_tokens_generated(self) -> int:
        n = 0
        for t in self.turns:
            llm = t.get("llm") or {}
            n += int(llm.get("generated_tokens") or t.get("generated_tokens") or 0)
        return n

    def _avg(self, key: str) -> float | None:
        vals = []
        for t in self.turns:
            v = t.get(key)
            if v is None and isinstance(t.get("llm"), dict):
                v = t["llm"].get(key)
            if v is not None:
                vals.append(float(v))
        if not vals:
            return None
        return statistics.mean(vals)

    def _values(self, key: str) -> list[float]:
        vals: list[float] = []
        for t in self.turns:
            v = t.get(key)
            if v is None and isinstance(t.get("llm"), dict):
                v = t["llm"].get(key)
            if v is not None:
                vals.append(float(v))
        return vals

    @property
    def avg_power_w(self) -> float | None:
        if not self._rolling_power_w:
            return None
        return statistics.mean(self._rolling_power_w)

    @property
    def peak_power_w(self) -> float | None:
        if not self._rolling_power_w:
            return None
        return max(self._rolling_power_w)

    @property
    def idle_power_w(self) -> float | None:
        if not self._rolling_power_w:
            return None
        return min(self._rolling_power_w)

    @property
    def tokens_per_joule(self) -> float | None:
        avg_p = self.avg_power_w
        if not avg_p or avg_p <= 0:
            return None
        total_tokens = self.total_tokens_generated
        total_time_s = sum(end - start for start, end in self._lock_ts)
        if total_time_s <= 0 or total_tokens <= 0:
            return None
        energy = avg_p * total_time_s
        if energy <= 0:
            return None
        return total_tokens / energy

    @property
    def estimated_tops(self) -> float | None:
        flops_per_token = self.model_params_b * 2e9 * 2
        avg_tps = self._avg("tok_s") or self._avg("decode_tok_s")
        if not avg_tps or avg_tps <= 0:
            return None
        return flops_per_token * avg_tps / 1e12

    def summary(self) -> dict[str, Any]:
        last = self.turns[-1] if self.turns else {}
        llm = last.get("llm") or {}
        latency_percentiles = {
            "ttft_ms": summarize_latency(self._values("ttft_ms")),
            "decode_tok_s": summarize_latency(self._values("decode_tok_s")),
            "ttfa_ms": summarize_latency(self._values("ttfa_ms")),
            "overlap_ms": summarize_latency(self._values("overlap_ms")),
        }
        return {
            "model_params_b": self.model_params_b,
            "turns": len(self.turns),
            "inferences": len(self.turns),
            "total_tokens_generated": self.total_tokens_generated,
            "avg_tokens_per_second": _r(self._avg("tok_s")),
            "avg_decode_tokens_per_second": _r(self._avg("decode_tok_s")),
            "avg_ttft_ms": _r(self._avg("ttft_ms")),
            "avg_ttfa_ms": _r(self._avg("ttfa_ms")),
            "avg_overlap_ms": _r(self._avg("overlap_ms")),
            "latency_percentiles": latency_percentiles,
            "avg_power_w": _r(self.avg_power_w),
            "peak_power_w": _r(self.peak_power_w),
            "idle_power_w": _r(self.idle_power_w),
            "tokens_per_joule": _r(self.tokens_per_joule),
            "estimated_tops": _r(self.estimated_tops),
            "rolling_tok_s": _r(self._rolling_tok_s[-1] if self._rolling_tok_s else None),
            "last_turn": {
                "token_latency_p50_ms": llm.get("token_latency_p50_ms")
                or last.get("token_latency_p50_ms"),
                "token_latency_p95_ms": llm.get("token_latency_p95_ms")
                or last.get("token_latency_p95_ms"),
                "token_latency_p99_ms": llm.get("token_latency_p99_ms")
                or last.get("token_latency_p99_ms"),
                "ttfa_ms": last.get("ttfa_ms"),
                "overlap_ms": last.get("overlap_ms"),
                "barge_in": last.get("barge_in"),
            },
        }
