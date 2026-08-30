"""OpenRouter tool turns must attribute tool execution time."""
from __future__ import annotations

import time

from nova_hailo.metrics import TurnMetrics


def test_turn_metrics_accumulates_tool_ms():
    m = TurnMetrics()
    assert m.tool_ms is None
    for _ in range(2):
        t0 = time.perf_counter()
        time.sleep(0.01)
        m.tool_ms = (m.tool_ms or 0.0) + (time.perf_counter() - t0) * 1000.0
    assert m.tool_ms >= 20.0
    assert m.to_dict()["tool_ms"] >= 20.0


def test_turn_metrics_llm_calls_serialize_per_round():
    m = TurnMetrics()
    m.llm_calls.append({"ttft_ms": 978.3, "llm_total_ms": 1000.0})
    m.llm_calls.append({"ttft_ms": 754.2, "llm_total_ms": 800.0})
    d = m.to_dict()
    assert len(d["llm_calls"]) == 2
    assert d["llm_calls"][0]["ttft_ms"] == 978.3
