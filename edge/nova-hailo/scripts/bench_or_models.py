"""TTFT + reply-quality bake-off across OpenRouter candidates.

Latency alone must not choose the default voice model, so this prints the
spoken reply next to the timing for every prompt and leaves the call to a human.

Usage (on the Pi):
    .venv/bin/python scripts/bench_or_models.py --rounds 5
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

from nova_hailo.backends.openrouter_llm import build_provider_block

URL = "https://openrouter.ai/api/v1/chat/completions"

CANDIDATES = [
    "thinkingmachines/inkling-small",
    "deepseek/deepseek-v4-flash-0731",
    "meta-llama/llama-3.3-70b-instruct",
    "z-ai/glm-4.7-flash",
    "google/gemini-2.5-flash-lite",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-4.1-nano",
]

SYSTEM = (
    "You are Nova, a concise in-car voice assistant. Answer in one short "
    "spoken sentence. Never use lists, markdown, or emoji."
)
PROMPTS = [
    "What's a good way to stay awake on a long drive?",
    "How much charge do I need to reach Coimbatore?",
    "Remind me why the service light might be on.",
]


# Below this many samples, v[int(n * 0.95)] resolves to the last index --
# i.e. _pct(v, 0.95) is arithmetically identical to max(v). A real p95 needs
# n >= 20; below that we report it honestly as "not yet meaningful" rather
# than printing a number that looks like a percentile but is actually a max.
P95_MIN_N = 20


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    return v[min(int(len(v) * q), len(v) - 1)]


def measure(
    client: httpx.Client, key: str, model: str, prompt: str, *, max_tokens: int = 80
) -> tuple[float | None, str]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "reasoning": {"enabled": False},
        "session_id": "nova-bench",
    }
    block = build_provider_block(sort="latency", preferred_max_latency_p90=None)
    if block:
        body["provider"] = block
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    ttft: float | None = None
    parts: list[str] = []
    # A model can be rate-limited (HTTP 429), timeout, or drop the connection
    # mid-stream; any of those must be recorded as "unavailable" for this
    # model and the run must continue to the next candidate, never abort.
    try:
        with client.stream("POST", URL, headers=headers, json=body) as r:
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                piece = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                if piece:
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000.0
                    parts.append(piece)
    except httpx.HTTPError as exc:
        return None, f"ERROR: {exc.__class__.__name__}: {exc}"
    return ttft, "".join(parts).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--out", default="logs/or_model_bench.json")
    args = ap.parse_args()

    key = (
        os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OR_API_KEY") or ""
    ).strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    client = httpx.Client(timeout=60.0)

    # Prime the keep-alive connection before the timed loop starts. Without
    # this, the very first request of the whole run -- round 0,
    # CANDIDATES[0] -- pays DNS + TCP + TLS handshake time inside its
    # measured TTFT window, while every other request (any later model, any
    # later round) reuses the now-warm connection. That systematically
    # penalises whichever model happens to be listed first. This call's
    # result is discarded entirely -- it never touches `samples` or
    # `replies` -- and a failure here must not abort the run.
    print("  warming connection...", flush=True)
    try:
        measure(client, key, CANDIDATES[0], PROMPTS[0], max_tokens=8)
    except Exception:
        pass

    samples: dict[str, list[float]] = {m: [] for m in CANDIDATES}
    replies: dict[str, list[str]] = {m: [] for m in CANDIDATES}
    # Interleave models within each round so time-of-day load hits every
    # candidate equally; measuring them in blocks would bias the first one.
    for rnd in range(args.rounds):
        for model in CANDIDATES:
            prompt = PROMPTS[rnd % len(PROMPTS)]
            ttft, text = measure(client, key, model, prompt)
            if ttft is not None:
                samples[model].append(ttft)
            if rnd == 0:
                replies[model].append(f"{prompt}\n      -> {text}")
            time.sleep(0.3)
        print(f"  round {rnd + 1}/{args.rounds}", flush=True)

    results: dict[str, dict] = {}
    any_below_p95_min_n = False
    print(f"\n{'model':<38}{'n':>4}{'p50':>8}{'max':>8}{'p95':>8}{'min':>8}")
    for model in CANDIDATES:
        v = samples[model]
        n = len(v)
        max_v = max(v) if v else None
        # A real p95 needs n >= P95_MIN_N samples (see _pct's docstring
        # comment); below that it would just be a relabelled max(v), so we
        # report it as null/n/a instead of a number that overstates itself.
        p95_v = _pct(v, 0.95) if n >= P95_MIN_N else None
        if v and n < P95_MIN_N:
            any_below_p95_min_n = True
        results[model] = {
            "n": n,
            "p50_ms": _pct(v, 0.50),
            "p95_ms": p95_v,
            "max_ms": max_v,
            "min_ms": min(v) if v else None,
            "sample_replies": replies[model],
        }
        if v:
            p95_cell = f"{p95_v:>8.0f}" if p95_v is not None else f"{'n/a':>8}"
            print(
                f"{model:<38}{n:>4}{_pct(v, 0.50):>8.0f}"
                f"{max_v:>8.0f}{p95_cell}{min(v):>8.0f}"
            )
        else:
            print(f"{model:<38}{'--':>4}{'unavailable':>32}")

    if any_below_p95_min_n:
        print(
            f"\nnote: p95 shown as 'n/a' where n < {P95_MIN_N} -- at fewer "
            "samples it would just be a relabelled max, not a real 95th "
            f"percentile. Use --rounds {P95_MIN_N} or more for a meaningful p95."
        )

    print("\n── sample replies (judge quality here) ──")
    for model in CANDIDATES:
        for r in results[model]["sample_replies"]:
            print(f"\n  [{model}]\n      {r}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
