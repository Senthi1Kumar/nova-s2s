"""Load the staged Gemma-4-E2B-it model and run one prompt through it.

Usage: uv run python scripts/engine_smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from nova.engine.wrapper import NovaEngine

MODEL_PATH = Path(__file__).resolve().parent.parent / "runtime" / "models" / "gemma-4-E2B-it.litertlm"
PROMPT = "What is 2+2? Answer with just the number."


def main() -> int:
    print(f"Loading model from {MODEL_PATH} ...")
    try:
        engine = NovaEngine.load(MODEL_PATH)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"Loaded in {engine.load_seconds:.2f}s")
    print(f"Prompt: {PROMPT!r}")

    result = engine.generate(PROMPT, max_output_tokens=32)
    engine.close()

    print(f"Response: {result.text!r}")
    print(f"Generate time: {result.generate_seconds:.2f}s")

    if not result.text.strip():
        print("FAIL: model returned an empty response", file=sys.stderr)
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
