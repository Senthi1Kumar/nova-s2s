#!/usr/bin/env bash
# CabinEnv with the laptop as the cabin screen: the pipeline runs on the Pi (scripts/run_demo_oem.sh there) and
# this opens the cockpit here (cb_lvgl --live, full screen). The cockpit reaches the Pi's /v1/observe through an
# SSH tunnel, so the Pi sees a loopback client: no TLS, no token. Ctrl-C stops everything.
#   cb_lvgl/scripts/run_cabinenv_laptop.sh            # voice through nova-connect in the browser (LiveKit)
#   cb_lvgl/scripts/run_cabinenv_laptop.sh --client   # voice through /v1/realtime with this laptop's mic/speaker
#                                                     # (headphones are best; NOVA_LAPTOP_AEC=1 adds PipeWire echo cancel)
# The client does not claim echo cancellation, so the Pi keeps the mic closed while Nova speaks: interrupt with the
# cockpit's x or mute button, or "Hey Nova". NOVA_LAPTOP_MIC_GAIN=<percent> sets the input volume after the echo
# canceller for a quiet desk mic (the server's speech gate wants rms >= 0.016); gain before the canceller clips the
# signal and defeats it.
# CB_LAYOUT=v2 opens the v0.0.2 avatar cockpit instead of layout B. PI (default RPi5-AI-HAT+2) is the ssh host; arguments after --client go to the voice client. The tunnel uses its
# own local port (LOCAL_PORT, default 18766) so it cannot collide with a LocalForward 8766 in ~/.ssh/config.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CB=$(cd "$HERE/.." && pwd)
NOVA=${NOVA_HAILO_DIR:-$(cd "$CB/.." && pwd)}
PI=${PI:-RPi5-AI-HAT+2}
PORT=${LOCAL_PORT:-18766}
CLIENT=0
if [ "${1:-}" = --client ]; then CLIENT=1; shift; fi
PIDS=()
MOD=""
SRC0=""
SNK0=""
MICSRC=""
VOL0=""
cleanup() {
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  if [ -n "$MOD" ]; then
    pactl set-default-source "$SRC0" 2>/dev/null || true
    pactl set-default-sink "$SNK0" 2>/dev/null || true
    pactl unload-module "$MOD" 2>/dev/null || true
  fi
  [ -n "$VOL0" ] && pactl set-source-volume "$MICSRC" "$VOL0" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
  echo "port $PORT is already open here: using it (an existing tunnel?)"
else
  # forwards from ~/.ssh/config for this host may come along; only ours (checked below) matters
  ssh -N -o ServerAliveInterval=15 -L "$PORT:127.0.0.1:8766" "$PI" 2>/dev/null &
  PIDS+=($!)
fi

for i in $(seq 1 120); do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/"; then break; fi
  [ "$i" = 1 ] && echo "waiting for the Nova pipeline on the Pi (scripts/run_demo_oem.sh) ..."
  [ "$i" = 120 ] && { echo "no pipeline answering through the tunnel after 120 s" >&2; exit 1; }
  sleep 1
done
echo "pipeline reachable through the tunnel"

# CB_ARGS adds cockpit flags (CB_ARGS=--perf logs the UI's refresh rate); CB_MAP_STATS=1 logs the map's.
# shellcheck disable=SC2086
NOVA_ENV_FILE=${NOVA_ENV_FILE:-$NOVA/.env} "$HERE/with_mapbox.sh" "$CB/build-sw/cb_lvgl" --fullscreen \
  --layout "${CB_LAYOUT:-b}" --live "ws://127.0.0.1:$PORT/v1/observe" ${CB_ARGS:-} > /tmp/cb_lvgl_laptop.log 2>&1 &
UI=$!
PIDS+=($UI)
echo "cockpit started (log /tmp/cb_lvgl_laptop.log)"

if [ "$CLIENT" = 0 ]; then
  echo "talk to Nova from the nova-connect page in the browser; Ctrl-C here closes the cockpit"
  wait "$UI"
  exit 0
fi

ARGS=(--url "ws://127.0.0.1:$PORT/v1/realtime" --wake none --device pipewire)
if [ "${NOVA_LAPTOP_AEC:-0}" = 1 ]; then
  # PipeWire's WebRTC echo canceller around the current mic and speaker, made the default for this session only.
  # It trims what leaks past the server's mic gate; no --aec, so the Pi still closes the mic while Nova speaks.
  SRC0=$(pactl get-default-source)
  SNK0=$(pactl get-default-sink)
  MOD=$(pactl load-module module-echo-cancel aec_method=webrtc source_master="$SRC0" sink_master="$SNK0" \
        source_name=nova_ec_source sink_name=nova_ec_sink)
  sleep 0.5
  pactl set-default-source nova_ec_source
  pactl set-default-sink nova_ec_sink
  echo "echo cancellation on: $SRC0 -> nova_ec_source, $SNK0 -> nova_ec_sink (restored on exit)"
fi
if [ -n "${NOVA_LAPTOP_MIC_GAIN:-}" ]; then   # after the canceller (when on): it needs the unclipped signal
  MICSRC=$(pactl get-default-source)
  [ "$MICSRC" != nova_ec_source ] && VOL0=$(pactl get-source-volume "$MICSRC" | grep -o '[0-9]*%' | head -1)
  pactl set-source-volume "$MICSRC" "${NOVA_LAPTOP_MIC_GAIN}%"
  echo "mic gain: $MICSRC -> ${NOVA_LAPTOP_MIC_GAIN}%${VOL0:+ (was $VOL0, restored on exit)}"
fi
echo "speak to Nova here (mic: $(pactl get-default-source)); close any nova-connect tab first: one conversation at a time"
"$NOVA/.venv/bin/python" "$NOVA/examples/realtime/python/nova_client.py" "${ARGS[@]}" "$@"
