#!/usr/bin/env python3
"""Measure llama-server prefix-cache behavior for Nova's turn shape.

Sends chat completions that share a long, byte-identical system prefix (like
Nova's real system+tools block) with a short varying user turn, and prints the
server-reported timings. With prefix caching working, call 2+ should show
cache_n > 0 and a prompt_ms that is a small fraction of call 1's.

Usage: uv run python scripts/prefill_probe.py [--base-url http://127.0.0.1:8080]
Requires a running llama-server (e.g. `uv run python scripts/serve_llm.py <profile>`).
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

# ~1800 tokens of stable prefix, mimicking Nova's system + 13 tool schemas.
FILLER_TOOL_DOC = (
    "Tool schema: name, description, JSON-schema parameters with typed "
    "properties, enums, required fields, and usage guidance for the model. "
)
SYSTEM_PROMPT = (
    "You are Nova, the in-vehicle voice assistant. Short sentences. "
    "Confirm irreversible actions. Never invent numbers.\n" + FILLER_TOOL_DOC * 90
)


def call(client: httpx.Client, base_url: str, user_text: str) -> dict:
    resp = client.post(
        f"{base_url}/v1/chat/completions",
        json={
            "model": "probe",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 8,
        },
        timeout=180.0,
    )
    resp.raise_for_status()
    return resp.json().get("timings", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    with httpx.Client() as client:
        try:
            client.get(f"{args.base_url}/health", timeout=3.0).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"FAIL: llama-server not reachable at {args.base_url} ({exc})")
            return 1
        results = []
        for i, user in enumerate(
            ["What's the weather like?", "Set the AC to 20 degrees.", "Any reminders today?"]
        ):
            t = call(client, args.base_url, user)
            results.append(t)
            print(
                f"call {i + 1}: prompt_n={t.get('prompt_n')} cache_n={t.get('cache_n')} "
                f"prompt_ms={t.get('prompt_ms', 0):.0f} "
                f"prefill_tok_s={t.get('prompt_per_second', 0) or 0:.0f} "
                f"decode_tok_s={t.get('predicted_per_second', 0) or 0:.0f}"
            )
        warm = results[1:]
        cached = all((t.get("cache_n") or 0) > 0 for t in warm)
        speedup = (results[0].get("prompt_ms") or 0) / max(
            1.0, max((t.get("prompt_ms") or 1) for t in warm)
        )
        print(f"\nprefix cache active on warm calls: {cached}")
        print(f"warm-call prefill speedup vs cold: {speedup:.1f}x")
        print(json.dumps(results, indent=2))
        return 0 if cached else 2


if __name__ == "__main__":
    sys.exit(main())
