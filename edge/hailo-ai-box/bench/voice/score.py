"""Scoring helpers for real AI Box voice benchmark results."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SMALL = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000}


def _numbers(words: list[str]) -> list[str]:
    numeric = set(_SMALL) | set(_TENS) | set(_SCALES)
    words = [
        word for i, word in enumerate(words)
        if word != "and" or i == 0 or i + 1 == len(words)
        or words[i - 1] not in numeric or words[i + 1] not in numeric
    ]
    out: list[str] = []
    i = 0
    while i < len(words):
        if words[i] not in _SMALL and words[i] not in _TENS and words[i] not in _SCALES:
            out.append(words[i])
            i += 1
            continue
        value = 0
        current = 0
        consumed = 0
        for word in words[i:]:
            if word in _SMALL:
                current += _SMALL[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(1, current) * 100
            elif word == "thousand":
                value += max(1, current) * 1000
                current = 0
            else:
                break
            consumed += 1
        out.append(str(value + current))
        i += consumed
    return out


def normalize_transcript(text: str) -> str:
    """Normalize casing/punctuation and English cardinal numbers for fair WER."""
    return " ".join(_numbers(_WORD.findall((text or "").lower())))


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalize_transcript(reference).split()
    hyp = normalize_transcript(hypothesis).split()
    if not ref:
        return float(bool(hyp))
    prev = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hw in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(ref)


def percentile(values: list[float | int | None], p: float) -> float | None:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(clean) - 1)
    return clean[lo] + (clean[hi] - clean[lo]) * (pos - lo)


NEGATIVE_CLIP_S = 10.0
LATENCY_METRICS = ("asr_ms", "llm_ttft_ms", "first_audio_ms", "total_ms")
GATE_CONDITIONS = ("quiet", "music")
WER_GATES = {"quiet": 0.10, "music": 0.20}
FALSE_TRIGGER_GATE_PER_10_MIN = 1.0
BYTES_OK_GATE = 1.0


def is_false_trigger(result: dict[str, Any]) -> bool:
    """A negative clip that reached ASR and came back with words."""
    return not result.get("ignored") and bool(normalize_transcript(result.get("transcript", "")))


def false_trigger_rate(negatives: list[dict[str, Any]]) -> dict[str, Any]:
    """Each entry is {"triggered": bool, "duration_s": float} for one negative clip."""
    triggers = sum(bool(n["triggered"]) for n in negatives)
    minutes = sum(n.get("duration_s", NEGATIVE_CLIP_S) for n in negatives) / 60
    return {
        "negatives": len(negatives),
        "minutes": minutes,
        "triggers": triggers,
        "rate": triggers / len(negatives) if negatives else None,
        "per_10_min": triggers / minutes * 10 if minutes else None,
    }


def _wer_stats(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    ref_words = 0
    errors = 0.0
    for reference, transcript in pairs:
        ref = normalize_transcript(reference).split()
        ref_words += len(ref)
        errors += word_error_rate(" ".join(ref), transcript) * max(1, len(ref))
    return {"clips": len(pairs), "ref_words": ref_words, "wer": errors / ref_words if ref_words else None}


def score_manifest(manifest_path: str | Path, results_path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text())
    results = json.loads(Path(results_path).read_text())
    by_id = {clip["id"]: clip for clip in results.get("clips", [])}
    missing = 0
    prompts: list[tuple[str, str]] = []
    prompts_by_condition: dict[str, list[tuple[str, str]]] = {}
    negatives: list[dict[str, Any]] = []
    negatives_by_condition: dict[str, list[dict[str, Any]]] = {}
    byte_flags: list[bool] = []
    errors = 0
    timings: dict[str, list[float]] = {}
    vad_states: dict[str, int] = {}
    for clip in manifest.get("clips", []):
        result = by_id.get(clip["id"])
        if result is None:
            missing += 1
            continue
        condition = clip.get("condition") or "unlabeled"
        if result.get("vad"):
            vad_states[result["vad"]] = vad_states.get(result["vad"], 0) + 1
        failed = result.get("status", "ok") != "ok"
        errors += failed
        if clip.get("kind") == "negative":
            if failed:
                continue
            entry = {"triggered": is_false_trigger(result),
                     "duration_s": clip.get("duration_s", NEGATIVE_CLIP_S)}
            negatives.append(entry)
            negatives_by_condition.setdefault(condition, []).append(entry)
        else:
            pair = (clip.get("reference", ""), result.get("transcript", ""))
            prompts.append(pair)
            prompts_by_condition.setdefault(condition, []).append(pair)
        if "bytes_ok" in result and not failed:
            byte_flags.append(bool(result["bytes_ok"]))
        for name, value in result.get("timings", {}).items():
            if isinstance(value, int | float):
                timings.setdefault(name, []).append(value)
    latency = {
        name: {"p50": percentile(values, 50), "p95": percentile(values, 95)}
        for name, values in sorted(timings.items())
    }
    unavailable = [name for name in ("cpu_percent", "rss_mb") if name not in timings]
    overall = _wer_stats(prompts)
    conditions = sorted(set(prompts_by_condition) | set(negatives_by_condition))
    return {
        "clips_total": len(manifest.get("clips", [])),
        "clips_completed": len(manifest.get("clips", [])) - missing,
        "clips_missing": missing,
        "errors": errors,
        "vad": dict(sorted(vad_states.items())),
        "wer": overall["wer"],
        "false_trigger": false_trigger_rate(negatives),
        "conditions": {
            name: {
                **_wer_stats(prompts_by_condition.get(name, [])),
                "false_trigger": false_trigger_rate(negatives_by_condition.get(name, [])),
            }
            for name in conditions
        },
        "bytes_ok_rate": sum(byte_flags) / len(byte_flags) if byte_flags else None,
        "latency_ms": latency,
        "unavailable_metrics": unavailable,
    }


def evaluate_gates(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """PASS/FAIL per gate; `passed` is None when the run holds no data for it.

    Meeting is reported in the summary only and is not gated.
    """
    conditions = summary.get("conditions", {})
    gates = []
    for name, limit in WER_GATES.items():
        value = conditions.get(name, {}).get("wer")
        gates.append({"gate": f"WER {name}", "value": value, "limit": limit,
                      "passed": None if value is None else value <= limit})
    groups = [conditions.get(name, {}).get("false_trigger", {}) for name in GATE_CONDITIONS]
    if all(g.get("minutes") for g in groups):
        value = sum(g["triggers"] for g in groups) / sum(g["minutes"] for g in groups) * 10
    else:
        value = None
    gates.append({"gate": "False triggers per 10 min", "value": value,
                  "limit": FALSE_TRIGGER_GATE_PER_10_MIN,
                  "passed": None if value is None else value <= FALSE_TRIGGER_GATE_PER_10_MIN})
    errors = summary.get("errors")
    gates.append({"gate": "errors", "value": errors, "limit": 0,
                  "passed": None if errors is None else errors == 0})
    rate = summary.get("bytes_ok_rate")
    gates.append({"gate": "bytes_ok", "value": rate, "limit": BYTES_OK_GATE,
                  "passed": None if rate is None else rate >= BYTES_OK_GATE})
    vad = summary.get("vad")
    if vad:
        gates.append({"gate": "VAD state (one, not failed)", "value": len(vad), "limit": 1,
                      "passed": len(vad) == 1 and "failed" not in vad})
    return gates


def vad_warning(summary: dict[str, Any]) -> str | None:
    """Explains why a run is invalid when its VAD state was mixed or failed."""
    vad = summary.get("vad") or {}
    counts = ", ".join(f"{name}={count}" for name, count in vad.items())
    if "failed" in vad:
        return (f"WARNING: INVALID RUN: the server VAD failed during this run ({counts}); "
                "its clips ran ungated and are not an 'after' result")
    if len(vad) > 1:
        return f"WARNING: INVALID RUN: clips ran with mixed VAD states ({counts})"
    return None


def _fmt(value: Any, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_gates(gates: list[dict[str, Any]]) -> str:
    lines = []
    for gate in gates:
        verdict = {True: "PASS", False: "FAIL", None: "NO DATA"}[gate["passed"]]
        lines.append(f"{gate['gate']:<28} {_fmt(gate['value']):>8}  (limit {gate['limit']:g})  {verdict}")
    overall = "PASS" if gates and all(g["passed"] for g in gates) else "FAIL"
    lines.append(f"{'Overall':<28} {'':>8}  {'':<14}{overall}")
    return "\n".join(lines)


def comparison_rows(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    rows: list[tuple[str, Any, Any]] = [("WER (all prompts)", a.get("wer"), b.get("wer"))]
    for name in sorted(set(a.get("conditions", {})) | set(b.get("conditions", {}))):
        ca, cb = a.get("conditions", {}).get(name, {}), b.get("conditions", {}).get(name, {})
        rows.append((f"WER {name}", ca.get("wer"), cb.get("wer")))
        rows.append((f"false triggers/10min {name}",
                     ca.get("false_trigger", {}).get("per_10_min"),
                     cb.get("false_trigger", {}).get("per_10_min")))
    rows.append(("false trigger rate", a.get("false_trigger", {}).get("rate"),
                 b.get("false_trigger", {}).get("rate")))
    rows.append(("bytes_ok rate", a.get("bytes_ok_rate"), b.get("bytes_ok_rate")))
    for metric in LATENCY_METRICS:
        for pct in ("p50", "p95"):
            rows.append((f"{metric} {pct}",
                         a.get("latency_ms", {}).get(metric, {}).get(pct),
                         b.get("latency_ms", {}).get(metric, {}).get(pct)))
    return rows


def format_comparison(a: dict[str, Any], b: dict[str, Any], labels: tuple[str, str]) -> str:
    width = max(len(labels[0]), len(labels[1]), 10)
    lines = [f"{'metric':<32} {labels[0]:>{width}} {labels[1]:>{width}}"]
    for name, left, right in comparison_rows(a, b):
        lines.append(f"{name:<32} {_fmt(left):>{width}} {_fmt(right):>{width}}")
    return "\n".join(lines)


def _load_summary(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if "summary" not in data:
        raise SystemExit(f"{path}: no summary; run bench_voice or pass --manifest")
    return data["summary"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print gates for bench_voice results, or compare two runs.")
    parser.add_argument("results", nargs="*", type=Path, help="bench_voice result JSON files")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                        help="side-by-side table of two result files")
    args = parser.parse_args(argv)
    if args.compare:
        a, b = args.compare
        print(format_comparison(_load_summary(a), _load_summary(b), (a.stem, b.stem)))
        return 0
    failed = False
    for path in args.results:
        summary = _load_summary(path)
        gates = evaluate_gates(summary)
        print(f"== {path.stem}")
        if warning := vad_warning(summary):
            print(warning)
        if vad := summary.get("vad"):
            print("VAD: " + ", ".join(f"{name}={count}" for name, count in vad.items()))
        print(format_gates(gates))
        failed = failed or not all(g["passed"] for g in gates)
    return 1 if failed or not args.results else 0


if __name__ == "__main__":
    sys.exit(main())
