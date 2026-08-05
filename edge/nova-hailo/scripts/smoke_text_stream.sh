#!/usr/bin/env bash
# Non-mic smoke: one text turn with streaming metrics
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/scripts/setup_env.sh"
export LLM_HEF="${LLM_HEF:-qwen2}"
python3 - <<'PY'
from nova_hailo.config import AppConfig
from nova_hailo.pipeline import NovaPipeline

cfg = AppConfig.load()
pipe = NovaPipeline(cfg=cfg, llm_hef="qwen2", text_only=True, no_tts=True)
try:
    r = pipe.run_text_turn("What time is it roughly?")
    print(r.metrics.pretty())
    d = r.metrics.to_dict()
    toks = (d.get("generated_tokens") or (d.get("llm") or {}).get("generated_tokens") or 0)
    assert int(toks) <= 24, toks
    print("SMOKE OK tokens=", toks)
finally:
    pipe.close()
PY
