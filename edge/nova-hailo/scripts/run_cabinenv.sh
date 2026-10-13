#!/usr/bin/env bash
# The whole CabinEnv demo with one command: the voice pipeline, then the mic and speaker
# client with the cockpit display beside it. Ctrl+C stops all of it.
#   scripts/run_cabinenv.sh               # open mic: everything said is a request
#   scripts/run_cabinenv.sh --wake-word   # say "Hey Nova" first
# NOVA_HAILO_WAKE_MODE (ptt = open mic, kws = wake word) set by hand wins over both.
# Pipeline log: /tmp/nova_server.log, cockpit log: /tmp/cb_lvgl.log.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${NOVA_HAILO_PORT:-8766}"
LOG="${NOVA_SERVER_LOG:-/tmp/nova_server.log}"
URL="http://127.0.0.1:$PORT/v1/session"

[[ -x .venv/bin/python && -x cb_lvgl/build-sw/cb_lvgl ]] || { echo "Run scripts/setup_pi.sh first." >&2; exit 1; }
mode="${NOVA_HAILO_WAKE_MODE:-ptt}"
args=()
for a in "$@"; do
  case "$a" in
    --wake-word) mode=kws ;;
    --open-mic) mode=ptt ;;
    *) args+=("$a") ;;
  esac
done
export NOVA_HAILO_WAKE_MODE="$mode"
if [[ "$mode" == kws ]]; then wake=(--wake server); else wake=(--wake none); fi

up() { curl -sf -o /dev/null "$URL"; }
if up; then
  echo "Voice pipeline already running on :$PORT; attaching to it (it keeps the wake mode it started with)."
else
  echo "Starting the voice pipeline (log $LOG); the first start takes about a minute..."
  # Its own process group, so Ctrl+C here stops the server and every child it started.
  setsid ./scripts/run_demo_oem.sh >"$LOG" 2>&1 &
  server=$!
  # The first signal starts a graceful shutdown that waits on open sessions; a later one forces it.
  stop_server() {
    kill -INT -- -"$server" 2>/dev/null || return 0
    for _ in $(seq 1 10); do kill -0 -- -"$server" 2>/dev/null || return 0; sleep 1; done
    kill -TERM -- -"$server" 2>/dev/null; sleep 2; kill -KILL -- -"$server" 2>/dev/null || true
  }
  trap stop_server EXIT
  for _ in $(seq 1 240); do
    up && break
    kill -0 "$server" 2>/dev/null || { echo "The pipeline stopped while starting:" >&2; tail -30 "$LOG" >&2; exit 1; }
    sleep 1
  done
  up || { echo "The pipeline did not come up in 4 minutes (log $LOG)." >&2; exit 1; }
fi
if [[ "$mode" == kws ]]; then
  echo "Ready. Say \"Hey Nova\", then your request. Ctrl+C to stop."
else
  echo "Ready. Open mic: just talk. Ctrl+C to stop."
fi
./scripts/run_cabinenv_client.sh "${wake[@]}" "${args[@]}"
