#!/usr/bin/env python3
"""Generate the Nova-Hailo v0.0.1 model matrix report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from model_inventory import DEFAULT_MANIFEST, load_manifest

from nova_hailo.config import ROOT

DEFAULT_REPORT = ROOT / "docs" / "benchmarks" / "nova-hailo-v0.0.1-model-matrix-report.md"


def read_json(path: Path) -> dict[str, Any] | list[Any]:
    return json.loads(path.read_text())


def table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = "| " + " | ".join(rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def latest_run_dir() -> Path:
    root = ROOT / "logs" / "model-matrix"
    runs = [p for p in root.glob("*") if p.is_dir() and (p / "summary.json").exists()]
    if not runs:
        raise FileNotFoundError("no model-matrix run dirs with summary.json")
    return sorted(runs)[-1]


def measured_label(summary: dict[str, Any]) -> str:
    return "MEASURED" if summary.get("measured") else "UNMEASURED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir()
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    manifest = load_manifest(args.manifest)
    summary = read_json(run_dir / "summary.json")
    inventory = read_json(run_dir / "inventory.json")
    components = {row["candidate_id"]: row for row in summary.get("components", [])}
    stable = manifest.get("profiles", {}).get("stable_candidate", {})

    matrix_rows = [["Candidate", "Component", "Manifest Support", "Inventory", "Measured", "Reason / Notes"]]
    for status in inventory:
        candidate_id = status["candidate_id"]
        comp = components.get(candidate_id, {})
        notes = "; ".join(comp.get("notes") or status.get("notes") or [])
        matrix_rows.append(
            [
                candidate_id,
                status.get("component") or "",
                next(
                    (
                        str(c.get("support"))
                        for c in manifest.get("candidates", [])
                        if c.get("id") == candidate_id
                    ),
                    "",
                ),
                status.get("support") or "",
                measured_label(comp) if comp else "NOT RUN",
                notes or status.get("reason") or "",
            ]
        )

    unsupported = {
        c["id"]: c.get("reason", "")
        for c in manifest.get("candidates", [])
        if c["id"] in {"asr_whisper_tiny_hailo", "asr_whisper_small_hailo"}
    }
    paper_notes = manifest.get("benchmark_protocol", {}).get("papers_methodology", [])
    raw_links = [
        f"- `{(run_dir / name).relative_to(ROOT)}`"
        for name in ["manifest.json", "inventory.json", "summary.json", "vad.jsonl", "asr.jsonl", "llm.jsonl", "tts.jsonl"]
        if (run_dir / name).exists()
    ]
    measured = [c for c in summary.get("components", []) if c.get("measured")]
    winner_lines = []
    best_by_comp: dict[str, dict] = {}
    for comp in ("vad", "asr", "llm", "tts", "wake"):
        rows = [c for c in measured if c.get("component") == comp]
        if not rows:
            winner_lines.append(f"- {comp}: no measured rows in this run")
            continue
        # prefer highest response_contract_pass_rate / lowest latency mean when present
        def score(c):
            q = c.get("quality") or {}
            lat = c.get("latency") or {}
            contract = q.get("response_contract_pass_rate")
            wer_mean = ((q.get("wer") or {}).get("mean"))
            first = ((lat.get("first_pcm_ms") or {}).get("p50"))
            ttft = ((lat.get("ttft_ms") or {}).get("p50"))
            return (
                0 if contract is None else -float(contract),
                0 if wer_mean is None else float(wer_mean),
                0 if ttft is None else float(ttft),
                0 if first is None else float(first),
            )
        best = sorted(rows, key=score)[0]
        best_by_comp[comp] = best
        winner_lines.append(
            f"- {comp}: `{best.get('candidate_id')}` (n={best.get('n_cases')}, ok={best.get('n_ok')}/{best.get('n_cases')})"
        )
    measured_winners_block = "\n".join(winner_lines) if winner_lines else "- none"
    # honest note
    measured_winners_block += "\n\nOnly rows with `measured=true` and real API fields are eligible. Unmeasured HEF-present stubs are ignored."

    def _mfmt(comp_row: dict) -> str:
        lat = comp_row.get("latency") or {}
        q = comp_row.get("quality") or {}
        res = comp_row.get("resources") or {}
        bits = []
        for key, label in (
            ("detection_latency_ms", "detect"),
            ("infer_ms", "infer"),
            ("ttft_ms", "TTFT"),
            ("decode_tok_s", "decode tok/s"),
            ("first_pcm_ms", "first-PCM"),
            ("cold_load_ms", "cold-load"),
        ):
            d = lat.get(key) or {}
            if d.get("p50") is not None:
                bits.append(f"{label} p50={d['p50']} p95={d.get('p95')}")
        for key, label in (("wer", "WER"), ("cer", "CER")):
            d = q.get(key) or {}
            if isinstance(d, dict) and d.get("p50") is not None:
                bits.append(f"{label} p50={d['p50']}")
        for key, label in (
            ("response_contract_pass_rate", "contract"),
            ("completeness_rate", "completeness"),
            ("false_accept_rate", "FAR"),
        ):
            if q.get(key) is not None:
                bits.append(f"{label}={q[key]}")
        rss = (res.get("rss_mb") or {}).get("max")
        if rss is not None:
            bits.append(f"RSS max={rss}MB")
        return "; ".join(bits) or "—"

    if best_by_comp:
        exec_table_rows = [["Stage", "Measured winner", "Key measured results (warm)"]]
        for comp in ("wake", "vad", "asr", "llm", "tts"):
            b = best_by_comp.get(comp)
            if b is None:
                continue
            exec_table_rows.append(
                [comp, f"`{b['candidate_id']}` (n={b.get('n_cases')})", _mfmt(b)]
            )
        exec_block = [
            f"**Measured stable profile (run `{summary.get('run_id')}`, "
            f"host `{(summary.get('host') or {}).get('hostname', '?')}`, "
            f"hardware={summary.get('hardware')}, dry_run={summary.get('dry_run')}):**",
            "",
            table(exec_table_rows),
            "",
        ]
        parts = []
        floor = 0.0
        for comp, key in (("asr", "infer_ms"), ("llm", "ttft_ms"), ("tts", "first_pcm_ms")):
            b = best_by_comp.get(comp)
            p50 = ((b or {}).get("latency", {}).get(key) or {}).get("p50")
            if p50 is not None:
                parts.append(f"{comp} {p50}")
                floor += float(p50)
        if len(parts) == 3:
            exec_block += [
                f"Warm component-sum lower bound: {' + '.join(parts)} ≈ **{floor:.0f} ms** "
                "before VAD endpointing, transport, and playback — a floor, not an E2E TTFA "
                "measurement. E2E audible TTFA requires the voice replay pass.",
                "",
            ]
    else:
        exec_block = [
            "Use the manifest stable profile as the pre-measurement recommendation when hardware rows are unmeasured:",
            "",
            table([["Stage", "Candidate"], *[[k, str(v)] for k, v in stable.items() if k != "note"]]),
            "",
            f"Stable profile note: {stable.get('note', 'n/a')}",
            "",
        ]

    lines = [
        "# Nova-Hailo v0.0.1 Model Matrix Report",
        "",
        "## Executive Recommendation",
        "",
        *exec_block,
        "This report is honest about `UNMEASURED` rows: dry-run and desk runs validate harness plumbing, corpus loading, manifest compatibility, and reporting, but they do not import hardware performance numbers.",
        "",
        "## Support And Compatibility Matrix",
        "",
        table(matrix_rows),
        "",
        "## Standalone Measured Winners",
        "",
        measured_winners_block,
        "",
        "## Unsupported ASR Notes",
        "",
        f"- `asr_whisper_tiny_hailo`: {unsupported.get('asr_whisper_tiny_hailo', 'No manifest reason found.')}",
        f"- `asr_whisper_small_hailo`: {unsupported.get('asr_whisper_small_hailo', 'No manifest reason found.')}",
        "",
        "## Pocket And Supertonic",
        "",
        "`tts_pocket` and `tts_supertonic` are included as experimental candidates. Pocket TTS is tracked as a PyTorch streaming runtime candidate; Supertonic is tracked as an ONNX fixed-voice candidate with OpenRAIL-M model licensing. Neither should be promoted from experimental without measured Pi/Hailo kit rows and license review.",
        "",
        "## Methodology Notes",
        "",
        "The local papers are used for methodology shape only, not imported absolute throughput or power numbers:",
    ]
    for item in paper_notes:
        lines.append(f"- `{item.get('path')}`: {item.get('use')}")
    e2e_summary_path = run_dir / "e2e_summary.json"
    e2e_block_lines = [
        "No E2E artifacts in this run directory. Run `scripts/bench_e2e_voice.py --out-dir <run-dir>` to add offline integration boundary rows.",
    ]
    if e2e_summary_path.exists():
        e2e_summary = read_json(e2e_summary_path)
        e2e_block_lines = [
            f"- dry_run: `{e2e_summary.get('dry_run')}`",
            f"- ttfa: `{e2e_summary.get('ttfa')}`",
            "- integrations: " + ", ".join(f"`{x}`" for x in (e2e_summary.get("integrations") or [])),
            "- status: offline mock only; not a Hailo hardware promotion gate.",
        ]
        for name in ("e2e.jsonl", "integration.jsonl", "e2e_summary.json", "wake.jsonl"):
            if (run_dir / name).exists():
                raw_links.append(f"- `{(run_dir / name).relative_to(ROOT)}`")

    lines += [
        "",
        "The bulk docs export under `hailo-docs/` is labeled HailoRT 5.3.x reference. The Nova-Hailo PoC path remains frozen on HailoRT 5.1.1.",
        "",
        "## Delivery Forecast",
        "",
        "- Reproducible measured matrix/report on target Pi: **3–5 focused working days** with uninterrupted Pi access and official 5.1.1 HEFs.",
        "- Software-first demo (simulated CAN/GPS, DriveAuth mock): **1–2 weeks after** benchmark/stabilization baseline.",
        "",
        "## E2E And Integration Boundaries",
        "",
        *e2e_block_lines,
        "",
        "## Raw Artifacts",
        "",
        *raw_links,
        "",
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
