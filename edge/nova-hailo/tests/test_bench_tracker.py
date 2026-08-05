"""Offline tests for hailo-ollama-bench style session tracker."""
from __future__ import annotations

from nova_hailo.bench.session_tracker import BenchTracker


def test_bench_summary_keys():
    b = BenchTracker(model_params_b=1.5)
    b.record_power(10.0)
    b.add_turn(
        {
            "ttft_ms": 320.0,
            "decode_tok_s": 8.2,
            "tok_s": 7.0,
            "ttfa_ms": 2600.0,
            "overlap_ms": 200.0,
            "llm_ms": 2000.0,
            "llm": {
                "generated_tokens": 10,
                "llm_total_ms": 2000.0,
                "token_latency_p50_ms": 120.0,
                "token_latency_p95_ms": 125.0,
            },
        }
    )
    s = b.summary()
    assert s["turns"] == 1
    assert s["avg_ttft_ms"] == 320.0
    assert s["avg_decode_tokens_per_second"] == 8.2
    assert s["latency_percentiles"]["ttft_ms"]["p50"] == 320.0
    assert s["latency_percentiles"]["decode_tok_s"]["p95"] == 8.2
    assert s["latency_percentiles"]["ttfa_ms"]["p99"] == 2600.0
    assert s["avg_power_w"] == 10.0
    assert s["last_turn"]["token_latency_p50_ms"] == 120.0
    assert s["tokens_per_joule"] is not None
    assert s["estimated_tops"] is not None
