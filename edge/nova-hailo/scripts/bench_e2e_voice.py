#!/usr/bin/env python3
"""Offline E2E replay bench for the Nova-Hailo voice path."""
from __future__ import annotations

import argparse
import importlib.util
import random
import time
from pathlib import Path
from typing import Any

from nova_hailo.bench.contracts import CaseResult, append_jsonl, sample_resources, write_json
from nova_hailo.config import ROOT


def hailo_available() -> bool:
    return importlib.util.find_spec("hailo_platform") is not None


def e2e_rows(seed: int, dry_run: bool) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    prompts = [
        "what is my next meeting",
        "find the latest project note",
        "tell me the cabin status",
        "confirm before sending payment",
        "summarize the route change",
    ]
    rows: list[dict[str, Any]] = []
    for i, prompt in enumerate(prompts, start=1):
        result = CaseResult(
            case_id=f"e2e_{i:03d}",
            candidate_id="stable_profile",
            component="e2e",
            ok=True,
            metrics={
                "ts": time.time(),
                "prompt": prompt,
                "ttfa_ms": None,
                "total_latency_ms": None,
                "audio_complete": None,
                "offline_unmeasured": dry_run,
                "mock_route_ms": round(12.0 + rng.random() * 8.0, 2),
            },
            resources=sample_resources().to_dict(),
            measured=False,
        )
        rows.append(result.to_dict())
    return rows


def integration_rows() -> list[dict[str, Any]]:
    names = ["tool_routing", "mcp_workspace_mock", "driveauth_mock", "can_obd_sim"]
    return [
        {
            "ts": time.time(),
            "integration": name,
            "ok": True,
            "measured": False,
            "mode": "mock",
            "notes": "offline mock row; hardware/live services not exercised",
        }
        for name in names
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dry_run = args.dry_run or not hailo_available()
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "logs" / "model-matrix" / (
        "e2e-" + time.strftime("%Y%m%d-%H%M%S", time.localtime())
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    append_jsonl(out_dir / "e2e.jsonl", e2e_rows(args.seed, dry_run))
    append_jsonl(out_dir / "integration.jsonl", integration_rows())
    write_json(
        out_dir / "e2e_summary.json",
        {
            "created_ts": time.time(),
            "out_dir": str(out_dir),
            "dry_run": dry_run,
            "ttfa": "unmeasured offline",
            "integrations": ["tool_routing", "mcp_workspace_mock", "driveauth_mock", "can_obd_sim"],
        },
    )
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
