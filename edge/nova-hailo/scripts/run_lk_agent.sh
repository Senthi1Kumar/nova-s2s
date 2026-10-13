#!/usr/bin/env bash
# LiveKit agent worker. Separate from run_demo_oem.sh on purpose: this does not
# start /v1/realtime. The two are independent front doors onto the same models.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/setup_env.sh" 2>/dev/null || true
export PYTHONPATH="${ROOT}/../..:${ROOT}:${PYTHONPATH:-}"

: "${LIVEKIT_URL:?set LIVEKIT_URL in .env}"
: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY in .env}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET in .env}"

echo "=========================================="
echo " Nova LiveKit agent worker"
echo "  SFU : ${LIVEKIT_URL}"
echo "  STT : parakeet (local)   TTS: piper (local)"
echo "=========================================="
exec "$ROOT/.venv/bin/python" -m nova_hailo.lk.agent "${@:-dev}"
