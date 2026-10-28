#!/usr/bin/env bash
# Run the Studio on one page for SECONDS, take a real full-screen capture with flameshot mid-run, and print
# FPS, thermals and load. Run on the laptop.   scripts/pi_live_shot.sh OUTDIR PAGE SECONDS [app args...]
set -euo pipefail
OUT=${1:?outdir}; PAGE=${2:?page}; SECS=${3:?seconds}; shift 3
PI=${PI:-RPi5-AI-HAT+2}
DEST=${DEST:-/home/cariad/repos/test_nova_repo/nova-s2s/edge/hailo-ai-box/studio_lvgl}
SSH="ssh -o ClearAllForwardings=yes"
EXTRA=; [ $# -gt 0 ] && EXTRA=$(printf '%q ' "$@")
mkdir -p "$OUT"
$SSH "$PI" "cd $DEST && rm -rf /tmp/aibox_shots && mkdir -p /tmp/aibox_shots && export XDG_RUNTIME_DIR=/run/user/\$(id -u) WAYLAND_DISPLAY=wayland-0 DISPLAY=:0 && \
  echo before: \$(vcgencmd measure_temp) \$(vcgencmd get_throttled) \$(vcgencmd measure_clock arm) load:\$(cut -d' ' -f1-3 /proc/loadavg) && \
  (scripts/run_on_pi.sh gpu --fullscreen --perf --page $PAGE --quit-after $SECS $EXTRA > /tmp/aibox_shots/run.log 2>&1 &) ; \
  sleep \$(( $SECS / 2 )); XDG_CURRENT_DESKTOP=sway QT_QPA_PLATFORM=wayland timeout 20 flameshot full -p /tmp/aibox_shots/ >/dev/null 2>&1; sleep \$(( $SECS / 2 + 3 )); \
  echo after: \$(vcgencmd measure_temp) \$(vcgencmd get_throttled) \$(vcgencmd measure_clock arm) load:\$(cut -d' ' -f1-3 /proc/loadavg); \
  grep sysmon /tmp/aibox_shots/run.log | tail -n +3 | awk '{n++; f+=\$2; if(min==\"\"||\$2<min)min=\$2} END{print \"UI sysmon samples\",n,\"avg FPS\",f/n,\"min\",min}'; \
  grep '^vision:' /tmp/aibox_shots/run.log | tail -n +3 | awk '{n++; f+=\$3} END{if(n)print \"preview fps avg\",f/n,\"over\",n,\"s\"}'; grep '^vision:' /tmp/aibox_shots/run.log | tail -2; ls /tmp/aibox_shots"
for f in $($SSH "$PI" "ls /tmp/aibox_shots/*.png" 2>/dev/null); do scp -q -o ClearAllForwardings=yes "$PI:$f" "$OUT/live_page${PAGE}_$(basename $f)"; done
ls "$OUT" | grep live_ || true
