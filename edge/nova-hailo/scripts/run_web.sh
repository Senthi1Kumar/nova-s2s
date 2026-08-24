#!/usr/bin/env bash
# Nova-Hailo web realtime (orb UI + webrtcvad + streaming cascade)
# Callers: operator on Pi. Starts uvicorn nova_hailo.web.app:app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/setup_env.sh" 2>/dev/null || true
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

export NOVA_HAILO_PORT="${NOVA_HAILO_PORT:-8766}"
export NOVA_HAILO_HOST="${NOVA_HAILO_HOST:-0.0.0.0}"
export NOVA_HAILO_PROFILE="${NOVA_HAILO_PROFILE:-oem}"
# Do NOT default LLM/TTS/PLAYBACK here: app.py lets env override config, so a
# default makes llm_hef/tts_engine/playback in the yaml permanently unreadable
# (this is what silently kept a playback: both -> browser edit from ever
# taking effect). Only export when the operator sets them explicitly for an A/B.
[[ -n "${NOVA_HAILO_PLAYBACK:-}" ]] && export NOVA_HAILO_PLAYBACK
[[ -n "${NOVA_HAILO_TTS:-}" ]] && export NOVA_HAILO_TTS
[[ -n "${NOVA_HAILO_LLM:-}" ]] && export NOVA_HAILO_LLM
cfgval() { grep -E "^[[:space:]]*$1:" "$NOVA_HAILO_CONFIG" 2>/dev/null | head -1 | sed "s/^[^:]*:[[:space:]]*//; s/[[:space:]]*#.*//; s/[\"']//g"; }

echo "=========================================="
echo " Nova-Hailo Web Realtime"
echo "  UI  : http://<pi-ip>:${NOVA_HAILO_PORT}/"
echo "  WS  : ws://<pi-ip>:${NOVA_HAILO_PORT}/v1/realtime"
echo "  Profile : ${NOVA_HAILO_PROFILE}"
echo "  LLM : ${NOVA_HAILO_LLM:-$(cfgval llm_hef)}${NOVA_HAILO_LLM:+ (env override!)}"
STT_ENGINE=$(grep -E "^[[:space:]]*stt_engine:" "$NOVA_HAILO_CONFIG" 2>/dev/null | awk '{print $2}')
echo "  STT : resident ${STT_ENGINE:-whisper_hef} (from ${NOVA_HAILO_CONFIG##*/}; NOVA_HAILO_SEQUENTIAL_STT=1 to A/B)"
echo "  VAD : ${NOVA_HAILO_VAD:-silero} (NOVA_HAILO_VAD=silero|firered|webrtc to A/B)"
echo "  TTS : ${NOVA_HAILO_TTS:-$(cfgval tts_engine)}${NOVA_HAILO_TTS:+ (env override!)} → ${NOVA_HAILO_PLAYBACK:-$(cfgval playback)}${NOVA_HAILO_PLAYBACK:+ (env override!)}"
echo "=========================================="
free -h || true

LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -n "${LAN:-}" ]]; then
  echo "Open: http://${LAN}:${NOVA_HAILO_PORT}/"
fi

# teed logs hide print() until a client connects unless unbuffered
export PYTHONUNBUFFERED=1
_nemo_lib="${ROOT}/models/nemo_speech"
if [[ -d "$_nemo_lib" ]]; then
  export LD_LIBRARY_PATH="${_nemo_lib}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec python3 -m uvicorn nova_hailo.web.app:app \
  --host "$NOVA_HAILO_HOST" \
  --port "$NOVA_HAILO_PORT" \
  --log-level info
