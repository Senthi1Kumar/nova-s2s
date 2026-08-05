"""Shared benchmark contracts: percentiles, resource samples, result rows."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNAVAILABLE = "unavailable"
EXPERIMENTAL = "experimental"
UNMEASURED = "unmeasured"


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile for p in [0, 100]."""
    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError(f"percentile must be in [0, 100], got {p}")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    rank = (p / 100.0) * (len(xs) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return xs[lo]
    w = rank - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def summarize_latency(values: Sequence[float], nd: int = 2) -> dict[str, float | None]:
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return {
            "n": 0,
            "mean": None,
            "stdev": None,
            "min": None,
            "max": None,
            "p50": None,
            "p95": None,
            "p99": None,
        }

    def _r(v: float | None) -> float | None:
        return None if v is None else round(float(v), nd)

    return {
        "n": len(xs),
        "mean": _r(statistics.mean(xs)),
        "stdev": _r(statistics.stdev(xs) if len(xs) > 1 else 0.0),
        "min": _r(min(xs)),
        "max": _r(max(xs)),
        "p50": _r(percentile(xs, 50)),
        "p95": _r(percentile(xs, 95)),
        "p99": _r(percentile(xs, 99)),
    }


def sha256_file(path: Path, chunk: int = 1 << 20) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


@dataclass
class ResourceSample:
    ts: float
    rss_mb: float | None = None
    mem_available_mb: float | None = None
    cpu_percent: float | None = None
    swap_used_mb: float | None = None
    cpu_temp_c: float | None = None
    power_w: float | None = None
    power_scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sample_resources(power_w: float | None = None, power_scope: str | None = None) -> ResourceSample:
    rss_mb = None
    cpu_percent = None
    mem_available_mb = None
    swap_used_mb = None
    cpu_temp_c = None
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        rss_mb = proc.memory_info().rss / (1024 * 1024)
        cpu_percent = proc.cpu_percent(interval=0.0)
        vm = psutil.virtual_memory()
        mem_available_mb = vm.available / (1024 * 1024)
        swap_used_mb = psutil.swap_memory().used / (1024 * 1024)
    except Exception:
        pass
    try:
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal.exists():
            cpu_temp_c = int(thermal.read_text().strip()) / 1000.0
    except Exception:
        pass
    return ResourceSample(
        ts=time.time(),
        rss_mb=round(rss_mb, 2) if rss_mb is not None else None,
        mem_available_mb=round(mem_available_mb, 2) if mem_available_mb is not None else None,
        cpu_percent=round(cpu_percent, 2) if cpu_percent is not None else None,
        swap_used_mb=round(swap_used_mb, 2) if swap_used_mb is not None else None,
        cpu_temp_c=round(cpu_temp_c, 2) if cpu_temp_c is not None else None,
        power_w=power_w,
        power_scope=power_scope,
    )


@dataclass
class CandidateStatus:
    candidate_id: str
    component: str
    support: str
    reason: str
    artifact_path: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    runtime: str | None = None
    backend: str | None = None
    license: str | None = None
    expected_memory_mb: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseResult:
    case_id: str
    candidate_id: str
    component: str
    ok: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    resources: dict[str, Any] | None = None
    cold: bool = False
    warmup: bool = False
    measured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComponentSummary:
    candidate_id: str
    component: str
    support: str
    n_cases: int
    n_ok: int
    n_fail: int
    latency: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    measured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def host_fingerprint() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "processor": platform.processor(),
    }


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


_WER_STRIP_RE = re.compile(r"[^\w\s']")


def normalize_text(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Punctuation must go before scoring: Parakeet/Nemotron emit punctuation and
    capitalization by design, so "Nova, what is my next meeting?" against the
    reference "nova what is my next meeting" scored WER 0.333 for a *perfect*
    transcription (measured 2026-07-30). Whisper's own normalizer strips
    punctuation for the same reason. Apostrophes are kept ("don't" != "dont").
    """
    t = _WER_STRIP_RE.sub(" ", (s or "").lower())
    return " ".join(t.split())


def wer(ref: str, hyp: str) -> float:
    """Simple word error rate (edit distance / ref words)."""
    r = normalize_text(ref).split()
    h = normalize_text(hyp).split()
    if not r:
        return 0.0 if not h else 1.0
    dp = list(range(len(h) + 1))
    for i, rw in enumerate(r, start=1):
        prev = dp[0]
        dp[0] = i
        for j, hw in enumerate(h, start=1):
            cur = dp[j]
            if rw == hw:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = cur
    return dp[-1] / len(r)


def cer(ref: str, hyp: str) -> float:
    r = list(normalize_text(ref).replace(" ", ""))
    h = list(normalize_text(hyp).replace(" ", ""))
    if not r:
        return 0.0 if not h else 1.0
    dp = list(range(len(h) + 1))
    for i, rc in enumerate(r, start=1):
        prev = dp[0]
        dp[0] = i
        for j, hc in enumerate(h, start=1):
            cur = dp[j]
            if rc == hc:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = cur
    return dp[-1] / len(r)


def response_contract_ok(text: str, *, max_chars: int = 220, forbid_tools: bool = True) -> dict[str, Any]:
    """Lightweight spoken-response contract for voice replies."""
    t = (text or "").strip()
    failures: list[str] = []
    if not t:
        failures.append("empty")
    if len(t) > max_chars:
        failures.append("too_long")
    lower = t.lower()
    for bad in ("<|", "<｜", "start header", "end header", "<think>", "</think>", "```"):
        if bad in lower or bad in t:
            failures.append(f"control_token:{bad}")
            break
    if forbid_tools and any(x in lower for x in ("i'll transfer", "payment sent", "funds moved")):
        failures.append("unsafe_action_claim")
    return {"ok": not failures, "failures": failures, "chars": len(t)}
