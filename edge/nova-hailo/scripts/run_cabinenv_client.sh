#!/usr/bin/env bash
# CabinEnv on the Pi: headless voice (mic and speaker through PipeWire's echo
# canceller, talking to the local pipeline over /v1/realtime) plus the cockpit
# display (cb_lvgl, which reads /v1/observe). Start it after run_demo_oem.sh is up.
#   scripts/run_cabinenv_client.sh                 # open mic (server config wake.mode: ptt)
#   scripts/run_cabinenv_client.sh --wake server   # say "Hey Nova" (wake.mode: kws)
#   scripts/run_cabinenv_client.sh --wake device   # Enter wakes Nova
# NOVA_CABINENV_UI=0 skips the display; NOVA_CABINENV_UI_CMD replaces its command.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/set_xvf3800_levels.sh" || true
if [[ "${NOVA_HAILO_AEC:-1}" != "0" ]]; then
  NOVA_AEC_QUIET=1 "$ROOT/scripts/setup_pipewire_aec.sh" || true
  # shellcheck source=/dev/null
  source "$ROOT/scripts/hmi_aec_env.sh"
fi

UI_PID=""
if [[ "${NOVA_CABINENV_UI:-1}" != "0" ]]; then
  # setup_pi.sh builds the sw renderer only; a gpu build is preferred when present.
  UI_R=sw
  [[ -x "$ROOT/cb_lvgl/build-gpu/cb_lvgl" ]] && UI_R=gpu
  UI_CMD="${NOVA_CABINENV_UI_CMD:-$ROOT/cb_lvgl/scripts/run_on_pi.sh $UI_R --fullscreen --live}"
  if [[ -n "${NOVA_CABINENV_UI_CMD:-}" || -x "$ROOT/cb_lvgl/build-$UI_R/cb_lvgl" ]]; then
    echo "CabinEnv display: $UI_CMD (log /tmp/cb_lvgl.log)"
    # The display has no audio and only observes, so it runs beside the voice client.
    $UI_CMD >/tmp/cb_lvgl.log 2>&1 &
    UI_PID=$!
    trap '[[ -n "$UI_PID" ]] && kill "$UI_PID" 2>/dev/null || true' EXIT
  else
    echo "CabinEnv display: cb_lvgl is not built (build-gpu or build-sw); voice only"
  fi
fi

"$ROOT/.venv/bin/python" "$ROOT/examples/realtime/python/nova_client.py" \
  --url "${NOVA_REALTIME_URL:-ws://127.0.0.1:8766/v1/realtime}" \
  --wake none --aec --device "${NOVA_HAILO_CAPTURE_DEVICE:-pulse}" "$@"
