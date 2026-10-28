#!/usr/bin/env bash
# Start hailo-ollama (if needed), the AI Box server, and optionally the LVGL Studio.
# Loads runtime/setup.env and optional .env secrets without printing them.
# Env: AIBOX_HTTPS_PORT (8443), AIBOX_HTTP_PORT (8442), AIBOX_FAKE=1 (no NPU, no ollama),
#      AIBOX_LVGL=0 to skip the LVGL Studio. Ctrl-C stops everything this script started.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
if [[ -f "$ROOT/runtime/setup.env" ]]; then
  set -a
  source "$ROOT/runtime/setup.env"
  set +a
fi
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi
HTTPS_PORT="${AIBOX_HTTPS_PORT:-8443}"
HTTP_PORT="${AIBOX_HTTP_PORT:-8442}"
LOGS="$ROOT/runtime/logs"
mkdir -p "$LOGS"
PIDS=()

cleanup() {
  trap - INT TERM EXIT
  for ((i=${#PIDS[@]}-1; i>=0; i--)); do
    kill -TERM "${PIDS[$i]}" 2>/dev/null || true
  done
  # bounded wait (~8 s), then SIGKILL whatever is still alive
  for ((t=0; t<80; t++)); do
    alive=0
    for p in "${PIDS[@]:-}"; do
      [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && alive=1
    done
    [[ "$alive" == 0 ]] && break
    sleep 0.1
  done
  for p in "${PIDS[@]:-}"; do
    [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

# 1. TLS cert
bash scripts/gen_cert.sh

# 2. hailo-ollama (never in fake mode; only if nothing listens on 127.0.0.1:8000)
if [[ "${AIBOX_FAKE:-0}" == "1" ]]; then
  echo "AIBOX_FAKE=1: skipping hailo-ollama"
elif (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; then
  echo "hailo-ollama already listening on 127.0.0.1:8000"
elif command -v hailo-ollama >/dev/null 2>&1; then
  HAILO_OLLAMA_VDEVICE_GROUP_ID=SHARED hailo-ollama >"$LOGS/ollama.log" 2>&1 &
  PIDS+=("$!")
  echo "started hailo-ollama pid ${PIDS[-1]} (log runtime/logs/ollama.log)"
else
  echo "WARNING: hailo-ollama not found on PATH; chat models will report failed" >&2
fi

# 3. AI Box server
RSID_LIB="${AIBOX_REALSENSE_LIB:-$HOME/repos/RealSenseID/build-device/lib}"
if [[ -d "$RSID_LIB" ]]; then
  export LD_LIBRARY_PATH="$RSID_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PYTHONPATH="$RSID_LIB${PYTHONPATH:+:$PYTHONPATH}"
fi
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
"$PY" -m aibox >"$LOGS/aibox.log" 2>&1 &
SERVER_PID=$!
PIDS+=("$SERVER_PID")
echo "started aibox pid $SERVER_PID (log runtime/logs/aibox.log)"

# 4. wait for /health (up to 120 s)
ok=0
for ((i=0; i<120; i++)); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "aibox exited early; tail of log:" >&2
    tail -n 30 "$LOGS/aibox.log" >&2
    exit 1
  fi
  if curl -fsS -m 2 "http://127.0.0.1:${HTTP_PORT}/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[[ "$ok" == 1 ]] || { echo "timed out waiting for /health" >&2; exit 1; }

# 5. LVGL Studio
LVGL_BIN="$ROOT/studio_lvgl/build-gpu/aibox_studio"
[[ -x "$LVGL_BIN" ]] || LVGL_BIN="$ROOT/studio_lvgl/build/aibox_studio"
if [[ "${AIBOX_LVGL:-1}" != "0" && -x "$LVGL_BIN" ]]; then
  "$ROOT/studio_lvgl/scripts/run_on_pi.sh" gpu --fullscreen --url "http://127.0.0.1:${HTTP_PORT}" >"$LOGS/lvgl.log" 2>&1 &
  PIDS+=("$!")
  echo "started LVGL Studio pid ${PIDS[-1]}"
fi

# 6. URL
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "AI Box ready:  https://${LAN_IP:-localhost}:${HTTPS_PORT}/   (loopback: http://127.0.0.1:${HTTP_PORT}/)"
wait "$SERVER_PID"
