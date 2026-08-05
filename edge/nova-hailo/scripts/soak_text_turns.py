#!/usr/bin/env python3
"""Automated text-turn soak: real HailoRT LLM inference, no mic/VAD/TTS.

Proxy for the DEMO_TODO.md "resident coexistence soak (RSS/swap/temp)" gate.
Does NOT satisfy the audible-TTFA or barge-in-stop gates -- those need real
speech through the mic and a human operator; this only stresses the resident
Qwen2 GenAI load path across volume to catch leaks/crashes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from nova_hailo.bench.contracts import sample_resources
from nova_hailo.config import AppConfig
from nova_hailo.pipeline import NovaPipeline

CORPUS = [
    "Hello Nova.",
    "What's on my calendar today?",
    "Any unread email?",
    "Search the web for Hailo AI news.",
    "What did I just ask about?",
    "Who are you?",
    "Thanks, that's all for now.",
    "What time is it roughly?",
    "Tell me about the weather.",
    "Never mind.",
]


def main() -> None:
    n_turns = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
        f"logs/demo/soak_text_only-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = AppConfig.load()
    pipe = NovaPipeline(cfg=cfg, text_only=True, no_tts=True)
    t_start = time.time()
    try:
        with out_path.open("w") as f:
            for i in range(n_turns):
                q = CORPUS[i % len(CORPUS)]
                t0 = time.perf_counter()
                r = pipe.run_text_turn(q)
                dt_ms = (time.perf_counter() - t0) * 1000
                res = sample_resources()
                row = {
                    "turn": i + 1,
                    "q": q,
                    "turn_ms": round(dt_ms, 1),
                    "metrics": r.metrics.to_dict(),
                    "resources": res.to_dict(),
                }
                f.write(json.dumps(row) + "\n")
                f.flush()
                print(
                    f"[{i + 1}/{n_turns}] {q!r} -> {dt_ms:.0f}ms "
                    f"rss={res.rss_mb}MB swap={res.swap_used_mb}MB temp={res.cpu_temp_c}C"
                )
    finally:
        pipe.close()
    print(f"done in {time.time() - t_start:.1f}s -> {out_path}")


if __name__ == "__main__":
    main()
