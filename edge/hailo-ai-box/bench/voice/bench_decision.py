"""Benchmark local D3 or explicitly selected hosted Jev on fixed turn judgments."""

from __future__ import annotations

import argparse
import json
import os
import resource
import time
import urllib.request
from pathlib import Path
from typing import Any

from .score import percentile


def decision_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    warm = [row["latency_ms"] for row in records[1:]]
    matched = [row for row in records if row.get("matches_expected") is not None]
    return {
        "cold_ms": records[0]["latency_ms"] if records else None,
        "warm_ms": {"p50": percentile(warm, 50), "p95": percentile(warm, 95)},
        "rss_mb_max": max((row["rss_mb"] for row in records), default=None),
        "answer_parity": (
            sum(bool(row["matches_expected"]) for row in matched) / len(matched) if matched else None
        ),
    }


def answers_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(key in actual and answers_match(actual[key], value) for key, value in expected.items())
    if isinstance(expected, bool) and isinstance(actual, dict) and actual.get("type") == "noul":
        return (float(actual["noul"]) >= 0.5) == expected
    if isinstance(expected, str) and isinstance(actual, dict) and actual.get("type") == "choice":
        return actual.get("choice") == expected
    return actual == expected


def _answers(backend: str, model: Any, item: dict[str, Any]) -> Any:
    if backend == "d3-edge":
        return model.system_one(state=item["state"], questions=item["questions"])["answers"]
    request = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"model": model, "state": item["state"], "questions": item["questions"]}).encode(),
        headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["answers"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["d3-edge", "jev"], default="d3-edge")
    parser.add_argument("--model", default=None, help="local path/HF id for D3 or versioned Jev model id")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.backend == "jev" and not os.environ.get("TYPESAFE_API_KEY"):
        parser.error("hosted Jev requires TYPESAFE_API_KEY in the environment")

    model: Any = args.model or ("vllm-sr/d3-edge" if args.backend == "d3-edge" else "jev-1.14.0")
    if args.backend == "d3-edge":
        from transformers import AutoModel

        model = AutoModel.from_pretrained(model, trust_remote_code=True)
    suite = json.loads(args.suite.read_text())
    records: list[dict[str, Any]] = []
    for _ in range(args.repeat):
        for item in suite["cases"]:
            started = time.perf_counter()
            answers = _answers(args.backend, model, item)
            records.append({
                "id": item["id"],
                "latency_ms": (time.perf_counter() - started) * 1000,
                "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "answers": answers,
                "matches_expected": answers_match(answers, item.get("expected", answers)),
            })
    report = {"backend": args.backend, "records": records, "summary": decision_summary(records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
