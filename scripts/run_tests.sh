#!/usr/bin/env bash
# Nova s2s verification entrypoint.
# LiteRT / LiteRT-LM paths and nova_ai/eval are frozen — never imported here.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MODE="${1:-fast}"

run_fast() {
  echo "==> fast (deterministic, no models/GPU)"
  uv run pytest -q \
    --ignore=tests/engine \
    --ignore=tests/audio \
    --ignore=tests/server/test_app.py \
    -m "not model and not live and not slow" \
    "$@"
}

run_component() {
  echo "==> component (Nova contracts + DriveAuth mock suite)"
  uv run pytest -q \
    --ignore=tests/engine \
    --ignore=tests/audio \
    --ignore=tests/server/test_app.py \
    tests/ \
    Drive_auth_edge/tests/ \
    -m "not model and not live and not slow" \
    "$@"
}

run_s2s_nova() {
  echo "==> Nova-owned s2s regressions (markup / forced args / ConnState)"
  PYTHONPATH="${ROOT}/cloned/speech-to-speech/src:${PYTHONPATH:-}" \
  uv run pytest -q --import-mode=importlib \
    cloned/speech-to-speech/tests/test_nova_forced_tool_args.py \
    cloned/speech-to-speech/tests/test_llm_utils.py \
    cloned/speech-to-speech/tests/test_nova_s2s_regressions.py \
    "$@"
}

run_eval() {
  echo "==> s2s-native eval corpus (mocked; no LiteRT)"
  uv run pytest -q eval/tests/ "$@"
}

run_integration() {
  echo "==> recorded replay + fake dual-LLM"
  uv run pytest -q tests/integration/ "$@"
}

case "$MODE" in
  fast)
    run_fast "${@:2}"
    ;;
  component)
    run_component "${@:2}"
    run_s2s_nova "${@:2}"
    ;;
  eval)
    run_eval "${@:2}"
    ;;
  all-local)
    run_component "${@:2}"
    run_s2s_nova "${@:2}"
    run_eval "${@:2}"
    run_integration "${@:2}"
    ;;
  model|live)
    echo "Mode '$MODE' is operator-run (stack/models required)."
    echo "  model: uv run pytest -m model -q"
    echo "  live:  start run_demo.py, then uv run python scripts/demo_e2e.py --live"
    echo "  dry:   uv run python scripts/demo_e2e.py   # no stack; DriveAuth + report"
    exit 2
    ;;
  *)
    echo "Usage: $0 {fast|component|eval|all-local|model|live}"
    exit 2
    ;;
esac
