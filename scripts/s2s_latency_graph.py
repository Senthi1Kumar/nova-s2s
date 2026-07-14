"""Fetch live s2s latency percentiles and render a matplotlib PNG.

Usage:
  uv run python scripts/s2s_latency_graph.py \\
    [--url http://localhost:8000] [--model-name lfm2.5-350m-f16]

Reads ``GET /api/metrics/s2s/percentiles`` (tool-service). Does not touch the
LiteRT ``scripts/latency_graph.py`` path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "runtime" / "graphs"
PERCENTILE_LABELS = ["p50", "p75", "p90", "p95", "p99"]
# Prefer wall-clock ms stages on the chart; tok/s is a different unit.
DEFAULT_PLOT_STAGES = (
    "asr_ms",
    "driveauth_ms",
    "router_ms",
    "ttft_ms",
    "tts_first_byte_ms",
    "ttfb_ms",
    "total_turn_ms",
)


def fetch_percentiles(url: str) -> dict[str, Any]:
    response = httpx.get(f"{url.rstrip('/')}/api/metrics/s2s/percentiles", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _per_turn_map(payload: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Accept either the s2s `{per_turn: ...}` envelope or a flat stage map."""
    per_turn = payload.get("per_turn")
    if isinstance(per_turn, dict):
        return per_turn
    # Flat map fallback (defensive).
    return {
        k: v
        for k, v in payload.items()
        if isinstance(v, dict) and any(p in v for p in PERCENTILE_LABELS)
    }


def render_graph(
    percentiles: dict[str, dict[str, float]],
    model_name: str,
    output_path: Path,
    *,
    stages: tuple[str, ...] = DEFAULT_PLOT_STAGES,
) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    x = range(len(PERCENTILE_LABELS))
    plotted = 0
    for stage in stages:
        stage_percentiles = percentiles.get(stage)
        if not stage_percentiles:
            continue
        values = [float(stage_percentiles.get(label, float("nan"))) for label in PERCENTILE_LABELS]
        ax.plot(x, values, marker="o", label=stage)
        plotted += 1

    if plotted == 0:
        # Fall back to whatever stages are present.
        for stage, stage_percentiles in percentiles.items():
            values = [float(stage_percentiles.get(label, float("nan"))) for label in PERCENTILE_LABELS]
            ax.plot(x, values, marker="o", label=stage)

    ax.set_xticks(list(x))
    ax.set_xticklabels(PERCENTILE_LABELS)
    ax.set_ylabel("milliseconds")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ax.set_title(f"Nova s2s latency percentiles — {model_name}\n{stamp}")
    ax.legend()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model-name", default="lfm2.5-350m-f16")
    parser.add_argument(
        "--output",
        default=None,
        help="PNG path (default: runtime/graphs/s2s_latency_<model>_<utc>.png)",
    )
    args = parser.parse_args()

    payload = fetch_percentiles(args.url)
    per_turn = _per_turn_map(payload)
    if not per_turn:
        print("No s2s metrics recorded yet — POST /metrics/s2s/turn first.")
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output_path = (
        Path(args.output)
        if args.output
        else OUTPUT_DIR / f"s2s_latency_{args.model_name}_{timestamp}.png"
    )
    render_graph(per_turn, args.model_name, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
