#!/usr/bin/env bash
# One-command launcher. Stack / profiles: docs/STACK.md
#
# Profiles (NOVA_HAILO_PROFILE):
#   oem            (default) → config.oem_v002_test.yaml  tools ON (search, research, Workspace)
#   conversation               → config.oem.yaml            tools gated, parakeet STT
#   oem_rollback               → config.oem_rollback.yaml   panic: no tools, Piper
#
# Explicit NOVA_HAILO_CONFIG always wins over profile defaults.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export NOVA_HAILO_PROFILE="${NOVA_HAILO_PROFILE:-oem}"
# Do NOT default these: app.py lets env override config, so a default here makes
# config yaml silently ineffective. Only export when the operator sets them for an A/B.
[[ -n "${NOVA_HAILO_TTS:-}" ]] && export NOVA_HAILO_TTS
[[ -n "${NOVA_HAILO_LLM:-}" ]] && export NOVA_HAILO_LLM
[[ -n "${NOVA_HAILO_STT:-}" ]] && export NOVA_HAILO_STT
[[ -n "${NOVA_HAILO_PLAYBACK:-}" ]] && export NOVA_HAILO_PLAYBACK
export NOVA_HAILO_PORT="${NOVA_HAILO_PORT:-8766}"
export NOVA_HAILO_HOST="${NOVA_HAILO_HOST:-0.0.0.0}"

if [[ -z "${NOVA_HAILO_CONFIG:-}" ]]; then
  case "$NOVA_HAILO_PROFILE" in
    oem_rollback)
      export NOVA_HAILO_CONFIG="$ROOT/config.oem_rollback.yaml"
      ;;
    conversation|oem_conversation|oem_v001)
      export NOVA_HAILO_CONFIG="$ROOT/config.oem.yaml"
      ;;
    oem|oem_demo|oem_v002|*)
      # Default: tools-enabled demo profile
      export NOVA_HAILO_CONFIG="$ROOT/config.oem_v002_test.yaml"
      ;;
  esac
fi

# shellcheck disable=SC1091
source "$ROOT/scripts/setup_env.sh" 2>/dev/null || true
export PYTHONPATH="${ROOT}/../..:${ROOT}:${PYTHONPATH:-}"

# Mixer state does not survive a reboot (alsactl store needs root), so re-apply
# capture gain + ALC on every start. Never fatal: the script exits 0 when the
# card is missing.
"$ROOT/scripts/set_wm8960_mic_gain.sh" "${WM8960_GAIN_PCT:-100}" || true

echo "=========================================="
echo " Nova-Hailo OEM Demo"
echo "  Profile : ${NOVA_HAILO_PROFILE}"
echo "  Config  : ${NOVA_HAILO_CONFIG}"
# Read the live config so the banner can never drift from what actually loads.
cfgval() { grep -E "^[[:space:]]*$1:" "$NOVA_HAILO_CONFIG" | head -1 | sed "s/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*//; s/[\"']//g"; }
echo "  LLM     : ${NOVA_HAILO_LLM:-$(cfgval llm_hef)}${NOVA_HAILO_LLM:+  (env override!)}"
echo "  STT     : ${NOVA_HAILO_STT:-$(cfgval stt_engine)}${NOVA_HAILO_STT:+  (env override!)}  decoder=$(cfgval parakeet_decoder)"
echo "  TTS     : ${NOVA_HAILO_TTS:-$(cfgval tts_engine)}${NOVA_HAILO_TTS:+  (env override!)}"
echo "  Playback: ${NOVA_HAILO_PLAYBACK:-$(cfgval playback)}${NOVA_HAILO_PLAYBACK:+  (env override!)}"
echo "  Tools   : $(cfgval profile)   Barge-in: $(cfgval barge_in_while_speaking)"
echo "  Rollback: NOVA_HAILO_PROFILE=oem_rollback $0"
echo "  Legacy  : NOVA_HAILO_PROFILE=conversation $0  (config.oem.yaml)"
echo "=========================================="

# Tee every run to a timestamped log so failures can be inspected after the
# fact (and over ssh) instead of scraped from a terminal scrollback.
LOG_DIR="$ROOT/logs/demo"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"
ln -sfn "$RUN_LOG" "$LOG_DIR/latest.log"
echo "  Log     : $RUN_LOG (tail -f logs/demo/latest.log)"
echo "=========================================="
exec "$ROOT/scripts/run_web.sh" 2>&1 | tee "$RUN_LOG"
