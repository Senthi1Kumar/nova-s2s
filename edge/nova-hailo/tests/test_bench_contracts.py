from __future__ import annotations

import pytest

from nova_hailo.bench.contracts import cer, percentile, response_contract_ok, summarize_latency, wer


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)


def test_wer_and_cer():
    assert wer("hello nova", "hello nova") == 0.0
    assert wer("hello nova", "hello there") == 0.5
    assert cer("abc", "axc") == pytest.approx(1 / 3)


def test_response_contract_ok_flags_control_tokens():
    ok = response_contract_ok("Sure, I can help.")
    bad = response_contract_ok("<think>hidden</think>")
    assert ok["ok"] is True
    assert bad["ok"] is False
    assert bad["failures"]


def test_summarize_latency_p_keys():
    summary = summarize_latency([10, 20, 30])
    assert summary["n"] == 3
    assert summary["p50"] == 20.0
    assert summary["p95"] == 29.0
    assert summary["p99"] == 29.8
