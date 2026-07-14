"""Lightweight scorers for the Nova s2s-native eval corpus.

No LiteRT / nova.engine imports. Predictions are plain dicts shaped like
``{"name": str|None, "args": dict, "speak": str, "call_id": str}`` or aggregates
thereof — produced by ``eval.run_corpus`` (route path / mocked agent).
"""
from __future__ import annotations

import math
import re
from typing import Any

from eval import AUTH_STATUS_ALIASES

# Same family as cloned/speech-to-speech LLM utils — keep eval self-contained.
_TOOL_MARKUP_RE = re.compile(
    r"<\|tool_call_start\|>.*?<\|tool_call_end\|>"
    r"|<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>"
    r"|<\|tool_call_start\|>|<\|tool_call_end\|>"
    r"|<\|tool_calls_section_begin\|>|<\|tool_calls_section_end\|>"
    r"|```\s*tool_call|invoke\s+\w+\s*\(",
    flags=re.DOTALL | re.IGNORECASE,
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def normalize_auth_status(status: str | None) -> str | None:
    if status is None:
        return None
    key = str(status).strip().lower()
    return AUTH_STATUS_ALIASES.get(key, key)


def score_tool_name(predicted: str | None, expected: str | None) -> bool:
    """Exact tool-name match. Both None (no-tool) counts as a hit."""
    p = (predicted or "").strip() or None
    e = (expected or "").strip() or None
    return p == e


def score_argument_slots(
    predicted_args: dict[str, Any] | None,
    expected_args: dict[str, Any] | None,
) -> dict[str, Any]:
    """Slot-wise compare: matched / missing / extra / wrong.

    Only keys present in ``expected_args`` are required. Extra predicted keys
    are reported but do not fail ``ok`` unless they collide with a wrong value
    on a required key.
    """
    pred = dict(predicted_args or {})
    exp = dict(expected_args or {})
    matched: list[str] = []
    missing: list[str] = []
    wrong: list[str] = []
    for key, want in exp.items():
        if key not in pred:
            missing.append(key)
            continue
        got = pred[key]
        if _args_equal(got, want):
            matched.append(key)
        else:
            wrong.append(key)
    extra = [k for k in pred if k not in exp]
    ok = not missing and not wrong
    return {
        "ok": ok,
        "matched": matched,
        "missing": missing,
        "wrong": wrong,
        "extra": extra,
        "precision": (len(matched) / len(pred)) if pred else 1.0,
        "recall": (len(matched) / len(exp)) if exp else 1.0,
    }


def _args_equal(got: Any, want: Any) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return got == want
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return math.isclose(float(got), float(want), rel_tol=0, abs_tol=1e-6)
    if isinstance(want, str) and isinstance(got, str):
        return got.strip().lower() == want.strip().lower()
    return got == want


def score_no_tool_pair(predicted_no_tool: bool, expected_no_tool: bool) -> dict[str, int]:
    """Single-example confusion counts for no-tool precision/recall aggregation."""
    tp = int(predicted_no_tool and expected_no_tool)
    fp = int(predicted_no_tool and not expected_no_tool)
    fn = int((not predicted_no_tool) and expected_no_tool)
    tn = int((not predicted_no_tool) and not expected_no_tool)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def aggregate_no_tool_pr(counts: list[dict[str, int]]) -> dict[str, float]:
    tp = sum(c["tp"] for c in counts)
    fp = sum(c["fp"] for c in counts)
    fn = sum(c["fn"] for c in counts)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def is_malformed_call(call: dict[str, Any] | None, known_tools: set[str] | None = None) -> bool:
    """True if the call cannot be executed safely / is structurally invalid."""
    if not isinstance(call, dict):
        return True
    name = call.get("name")
    if not isinstance(name, str) or not name.strip():
        return True
    if known_tools is not None and name not in known_tools:
        return True
    args = call.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return True
    for v in args.values():
        if isinstance(v, str) and has_forbidden_tool_markup(v):
            return True
    return False


def score_factual_speak(
    spoken: str,
    payload: dict[str, Any] | None,
    *,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Require numeric/factual payload values to appear literally in spoken text.

    Per Nova constraint #7: do not paraphrase tool numeric results. When
    ``keys`` is omitted, every int/float (and digit-bearing str) in the
    payload's top level is checked.
    """
    payload = payload or {}
    if keys is None:
        keys = [
            k
            for k, v in payload.items()
            if isinstance(v, (int, float))
            or (isinstance(v, str) and _NUM_RE.search(v))
        ]
    spoken_norm = (spoken or "").lower()
    missing: list[str] = []
    checked: list[str] = []
    for key in keys:
        if key not in payload:
            continue
        val = payload[key]
        checked.append(key)
        if isinstance(val, float):
            needles = {f"{val:g}", f"{val:.0f}", f"{val:.1f}", str(val)}
        else:
            needles = {str(val)}
        if not any(n.lower() in spoken_norm for n in needles if n):
            missing.append(key)
    return {"ok": not missing, "checked": checked, "missing": missing}


def has_forbidden_tool_markup(text: str | None) -> bool:
    """True if TTS/speak text still contains tool-call markup."""
    if not text:
        return False
    return bool(_TOOL_MARKUP_RE.search(text))


def score_duplicate_execution(calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Flag identical (name, frozen-args) pairs executed more than once in a turn."""
    calls = calls or []
    seen: dict[tuple[Any, ...], int] = {}
    dups: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name") or ""
        args = call.get("args") or {}
        if not isinstance(args, dict):
            key = (name, repr(args))
        else:
            key = (name, tuple(sorted((k, repr(v)) for k, v in args.items())))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            dups.append(str(name))
    return {"ok": not dups, "duplicates": dups, "call_count": len(calls)}


def score_auth_status(predicted: str | None, expected: str | None) -> bool:
    return normalize_auth_status(predicted) == normalize_auth_status(expected)


def latency_percentiles(
    samples_ms: list[float],
    ps: tuple[int, ...] = (50, 75, 90, 95, 99),
) -> dict[str, float]:
    """Nearest-rank percentiles over latency samples in milliseconds."""
    if not samples_ms:
        return {f"p{p}": 0.0 for p in ps}
    xs = sorted(float(x) for x in samples_ms)
    n = len(xs)
    out: dict[str, float] = {"n": float(n), "mean": round(sum(xs) / n, 3)}
    for p in ps:
        if n == 1:
            val = xs[0]
        else:
            rank = max(1, min(n, math.ceil(p / 100.0 * n)))
            val = xs[rank - 1]
        out[f"p{p}"] = round(val, 3)
    return out


def score_turn(
    *,
    expect: dict[str, Any],
    predicted_tool: str | None,
    predicted_args: dict[str, Any] | None = None,
    predicted_no_tool: bool | None = None,
    predicted_auth: str | None = None,
    speak: str | None = None,
    tool_payload: dict[str, Any] | None = None,
    calls: list[dict[str, Any]] | None = None,
    known_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Bundle per-turn scores used by ``run_corpus``."""
    exp_tool = expect.get("tool")
    exp_no_tool = bool(expect.get("no_tool") or expect.get("unsupported") or exp_tool is None)
    if "no_tool" in expect:
        exp_no_tool = bool(expect["no_tool"])
    if expect.get("unsupported"):
        exp_no_tool = True
    if predicted_no_tool is None:
        predicted_no_tool = predicted_tool is None

    if not exp_no_tool:
        tool_ok = score_tool_name(predicted_tool, exp_tool) and not predicted_no_tool
    else:
        tool_ok = bool(predicted_no_tool) or predicted_tool is None

    args_score = None
    if expect.get("args") is not None and not exp_no_tool:
        args_score = score_argument_slots(predicted_args, expect["args"])

    auth_ok = None
    if expect.get("auth_status") is not None:
        auth_ok = score_auth_status(predicted_auth, expect["auth_status"])

    markup = has_forbidden_tool_markup(speak)
    factual = None
    if tool_payload is not None and speak is not None:
        factual = score_factual_speak(speak, tool_payload)

    malformed = False
    if calls:
        malformed = any(is_malformed_call(c, known_tools) for c in calls)
    elif predicted_tool and not predicted_no_tool:
        malformed = is_malformed_call(
            {"name": predicted_tool, "args": predicted_args or {}}, known_tools
        )

    dup = score_duplicate_execution(calls)

    return {
        "tool_ok": tool_ok,
        "args": args_score,
        "no_tool": score_no_tool_pair(bool(predicted_no_tool), exp_no_tool),
        "auth_ok": auth_ok,
        "forbidden_markup": markup,
        "factual_speak": factual,
        "malformed": malformed,
        "duplicate": dup,
    }
