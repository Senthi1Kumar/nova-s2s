#!/usr/bin/env bash
# Measure cb_lvgl on the Pi desktop: start it, take two screenshots, sample CPU, stop it.
#   scripts/measure_on_pi.sh <gpu|sw> <seconds> <tag> [app args...]
# Writes /tmp/cb_<tag>_a.png, /tmp/cb_<tag>_b.png and /tmp/cb_<tag>.log; prints a summary.
set -euo pipefail
R=${1:?gpu|sw}; SECS=${2:?seconds}; TAG=${3:?tag}; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0

"$HERE/run_on_pi.sh" "$R" --fullscreen --perf "$@" > "/tmp/cb_$TAG.log" 2>&1 &
APP=$!
START=$(date +%s.%N)

# EVENTS: "<sec>:shot" or "<sec>:key=<xdotool key>" in time order, seconds after start; shots are
# /tmp/cb_<tag>_a.png, _b.png, ... Keys reach the gpu (Xwayland) build only.
LETTERS=(a b c d e f g h i j k l)
SHOT=0
for EV in ${EVENTS:-6:shot 7.3:shot}; do
  AT=${EV%%:*}
  WHAT=${EV#*:}
  sleep "$(awk -v a="$AT" -v s="$START" -v n="$(date +%s.%N)" 'BEGIN { d = a - (n - s); print (d > 0 ? d : 0) }')"
  case "$WHAT" in
    shot) grim -s 0.5 "/tmp/cb_${TAG}_${LETTERS[$SHOT]}.png"; SHOT=$((SHOT + 1)) ;;
    key=*) DISPLAY=:0 xdotool key "${WHAT#key=}" ;;
  esac
done
CPU=$("$HERE/cpu_sample.sh" "$APP" "$SECS" || echo "cpu_sample_failed")
ALIVE=$(kill -0 "$APP" 2>/dev/null && echo yes || echo no)
kill "$APP" 2>/dev/null || true
wait "$APP" 2>/dev/null || true

echo "tag=$TAG renderer=$R $(grep -m1 -o 'video=[a-z0-9]*' "/tmp/cb_$TAG.log") $CPU alive_at_end=$ALIVE"
grep -E 'sysmon: [0-9]+ FPS' "/tmp/cb_$TAG.log" | tail -n +3 | awk '
  { fps += $2; n++; for(i = 1; i <= NF; i++) if($i == "CPU") { c = $(i+1); sub("%", "", c); cpu += c } }
  END { if(n) printf "lvgl_avg_fps=%.1f lvgl_avg_cpu=%.1f%% samples=%d\n", fps/n, cpu/n, n }'
grep -E '\[(Warn|Error)\]' "/tmp/cb_$TAG.log" | sort | uniq -c | head -5 || true
