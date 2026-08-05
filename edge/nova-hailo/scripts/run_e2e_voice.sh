#!/usr/bin/env bash
# Full e2e voice: WM8960 → Whisper-Base → Llama3.2-1B → Piper (streaming)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/scripts/setup_env.sh"

HEF_LLM="${LLM_HEF:-qwen2}"
HEF_WHISPER="${WHISPER_HEF:-base}"

echo "=========================================="
echo " Nova-Hailo E2E Voice (streaming cascade)"
echo "  STT : Whisper (${HEF_WHISPER})"
echo "  LLM : ${HEF_LLM}"
echo "  TTS : Piper en_US-amy-low (stream)"
echo "  MIC : WM8960"
echo "=========================================="

# Host hygiene preflight (Chromium thrash kills latency)
if command -v free >/dev/null; then
  echo "--- memory ---"
  free -h || true
fi
swap_used=$(awk '/Swap:/ {print $3}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "${swap_used}" =~ ^[0-9]+$ ]] && (( swap_used > 500000 )); then
  echo "WARNING: swap heavily used (${swap_used} kB). Close Chromium / heavy apps before demo."
fi
if pgrep -x chromium >/dev/null 2>&1 || pgrep -x chromium-browser >/dev/null 2>&1; then
  echo "WARNING: Chromium is running — expect RAM thrash. Prefer a console session."
fi

python3 "${ROOT}/main.py" --list-audio

exec python3 "${ROOT}/main.py" \
  --llm-hef "${HEF_LLM}" \
  --whisper-hef "${HEF_WHISPER}" \
  --audio-device wm8960 \
  --metrics-json "${ROOT}/logs/turns.jsonl" \
  voice "$@"
