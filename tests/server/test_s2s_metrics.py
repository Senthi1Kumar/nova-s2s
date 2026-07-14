"""Tests for live s2s M1 metrics (collector + tool-service endpoints)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nova.server.s2s_metrics import (
    S2SMetricsCollector,
    compute_percentiles,
    s2s_metrics,
    sanitize_stages,
)
from nova.server.tool_service import app

pytestmark = [pytest.mark.fast, pytest.mark.component]


@pytest.fixture(autouse=True)
def _reset_s2s_metrics():
    s2s_metrics.clear()
    yield
    s2s_metrics.clear()


def test_compute_percentiles_known_values():
    values = [float(v) for v in range(1, 101)]
    result = compute_percentiles(values, percentiles=(50, 99))
    assert result["p50"] == 50.5
    assert result["p99"] == 99.01


def test_compute_percentiles_empty_returns_empty():
    assert compute_percentiles([]) == {}


def test_sanitize_stages_drops_missing_and_invalid():
    cleaned = sanitize_stages(
        {
            "asr_ms": 120.0,
            "ttfb_ms": None,
            "router_ms": "not-a-number",
            "decode_tok_s": 140.5,
            "unknown_stage": 999.0,
            "total_turn_ms": 2500,
        }
    )
    assert cleaned == {"asr_ms": 120.0, "decode_tok_s": 140.5, "total_turn_ms": 2500.0}
    assert "ttfb_ms" not in cleaned
    assert "unknown_stage" not in cleaned


def test_collector_percentiles_omit_missing_stages():
    collector = S2SMetricsCollector(max_turns=16)
    collector.record({"asr_ms": 100.0, "ttfb_ms": 800.0}, session_id="s1")
    collector.record({"asr_ms": 200.0}, session_id="s1")

    rollup = collector.percentiles(percentiles=(50,))
    assert "asr_ms" in rollup["per_turn"]
    assert "ttfb_ms" in rollup["per_turn"]
    assert rollup["per_turn"]["ttfb_ms"]["p50"] == 800.0
    assert "driveauth_ms" not in rollup["per_turn"]
    assert "router_ms" not in rollup["per_turn"]


def test_session_isolation_in_per_session_rollups():
    collector = S2SMetricsCollector(max_turns=32)
    collector.record({"ttfb_ms": 1000.0}, session_id="alpha")
    collector.record({"ttfb_ms": 2000.0}, session_id="alpha")
    collector.record({"ttfb_ms": 5000.0}, session_id="beta")

    all_sessions = collector.percentiles(percentiles=(50,))
    assert all_sessions["per_session"]["alpha"]["ttfb_ms"]["p50"] == 1500.0
    assert all_sessions["per_session"]["beta"]["ttfb_ms"]["p50"] == 5000.0
    assert all_sessions["per_turn"]["ttfb_ms"]["p50"] == 2000.0

    only_beta = collector.percentiles(percentiles=(50,), session_id="beta")
    assert set(only_beta["per_session"]) == {"beta"}
    assert "alpha" not in only_beta["per_session"]
    assert only_beta["per_session"]["beta"]["ttfb_ms"]["p50"] == 5000.0


def test_bounded_ring_evicts_oldest():
    collector = S2SMetricsCollector(max_turns=2)
    collector.record({"asr_ms": 1.0}, turn_id="t1", session_id="s")
    collector.record({"asr_ms": 2.0}, turn_id="t2", session_id="s")
    collector.record({"asr_ms": 3.0}, turn_id="t3", session_id="s")

    assert len(collector) == 2
    turn_ids = [r.turn_id for r in collector._turns]
    assert turn_ids == ["t2", "t3"]
    assert collector.session_turn_count("s") == 2


def test_pending_stages_merge_into_turn_record():
    collector = S2SMetricsCollector()
    collector.stash_stage("sess-1", "driveauth_ms", 42.5)
    collector.stash_stage("sess-1", "router_ms", 88.0)
    record = collector.record({"asr_ms": 10.0, "router_ms": 99.0}, session_id="sess-1")
    assert record.stages["driveauth_ms"] == 42.5
    assert record.stages["router_ms"] == 99.0
    assert record.stages["asr_ms"] == 10.0
    assert collector.take_pending("sess-1") == {}


def test_post_turn_and_get_percentiles_endpoints():
    client = TestClient(app)

    post = client.post(
        "/metrics/s2s/turn",
        json={
            "session_id": "live-1",
            "turn_id": "turn-a",
            "asr_ms": 150.0,
            "ttft_ms": 400.0,
            "ttfb_ms": 900.0,
            "total_turn_ms": 1800.0,
            "decode_tok_s": 120.0,
        },
    )
    assert post.status_code == 200
    body = post.json()
    assert body["turn_id"] == "turn-a"
    assert body["session_id"] == "live-1"
    assert body["stages"]["ttfb_ms"] == 900.0
    assert body["turn_count"] == 1

    client.post(
        "/metrics/s2s/turn",
        json={
            "session_id": "live-1",
            "asr_ms": 250.0,
            "ttfb_ms": 1100.0,
            "total_turn_ms": 2200.0,
        },
    )

    for path in ("/api/metrics/s2s/percentiles", "/metrics/s2s/percentiles"):
        resp = client.get(path)
        assert resp.status_code == 200
        data = resp.json()
        assert data["turn_count"] == 2
        assert "p50" in data["per_turn"]["ttfb_ms"]
        assert "p75" in data["per_turn"]["ttfb_ms"]
        assert "p90" in data["per_turn"]["ttfb_ms"]
        assert "p95" in data["per_turn"]["ttfb_ms"]
        assert "p99" in data["per_turn"]["ttfb_ms"]
        assert data["per_turn"]["ttfb_ms"]["p50"] == 1000.0
        assert "live-1" in data["per_session"]
        assert data["session_counts"]["live-1"] == 2


def test_endpoint_missing_stages_omitted_from_percentiles():
    client = TestClient(app)
    client.post("/metrics/s2s/turn", json={"session_id": "s", "asr_ms": 50.0})
    data = client.get("/api/metrics/s2s/percentiles").json()
    assert "asr_ms" in data["per_turn"]
    assert "ttfb_ms" not in data["per_turn"]
    assert "driveauth_ms" not in data["per_turn"]


def test_endpoint_session_filter_query_param():
    client = TestClient(app)
    client.post("/metrics/s2s/turn", json={"session_id": "a", "ttfb_ms": 100.0})
    client.post("/metrics/s2s/turn", json={"session_id": "b", "ttfb_ms": 900.0})

    data = client.get("/api/metrics/s2s/percentiles", params={"session_id": "a"}).json()
    assert set(data["per_session"]) == {"a"}
    assert data["per_session"]["a"]["ttfb_ms"]["p50"] == 100.0
    assert data["turn_count"] == 2


def test_nested_stages_dict_accepted():
    client = TestClient(app)
    resp = client.post(
        "/metrics/s2s/turn",
        json={
            "session_id": "nested",
            "stages": {"asr_ms": 11.0, "ttfb_ms": 22.0, "bogus": 1.0},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["stages"] == {"asr_ms": 11.0, "ttfb_ms": 22.0}
