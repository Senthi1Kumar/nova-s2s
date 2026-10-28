#!/usr/bin/env bash
# Run the Studio on the Pi display for a tour of all pages, log FPS + thermals, copy PNGs back. Run on the laptop.
#   scripts/pi_run_shot.sh OUTDIR [renderer=gpu] [extra app args...]
set -euo pipefail
OUT=${1:?outdir}; R=${2:-gpu}; shift 2 || true
PI=${PI:-RPi5-AI-HAT+2}
DEST=${DEST:-/home/cariad/repos/test_nova_repo/nova-s2s/edge/hailo-ai-box/studio_lvgl}
SSH="ssh -o ClearAllForwardings=yes"
EXTRA=; [ $# -gt 0 ] && EXTRA=$(printf '%q ' "$@")
mkdir -p "$OUT"
$SSH "$PI" "cd $DEST && rm -rf /tmp/studio_shots && mkdir -p /tmp/studio_shots && \
  echo thermal-before: \$(vcgencmd measure_temp) \$(vcgencmd get_throttled) \$(vcgencmd measure_clock arm) load:\$(cut -d' ' -f1-3 /proc/loadavg) && \
  timeout 90 scripts/run_on_pi.sh $R --fullscreen --perf --url http://127.0.0.1:7442 --tour /tmp/studio_shots --tour-ms ${TOUR_MS:-3500} $EXTRA 2>&1 | grep -v '^$' | tail -40; \
  echo thermal-after: \$(vcgencmd measure_temp) \$(vcgencmd get_throttled) \$(vcgencmd measure_clock arm) load:\$(cut -d' ' -f1-3 /proc/loadavg)"
for f in $($SSH "$PI" "ls /tmp/studio_shots/*.ppm" 2>/dev/null); do
  b=$(basename "$f" .ppm)
  $SSH "$PI" "python3 $DEST/scripts/ppm2png.py $f /tmp/studio_shots/$b.png" >/dev/null
  scp -q -o ClearAllForwardings=yes "$PI:/tmp/studio_shots/$b.png" "$OUT/$b.png"
done
ls "$OUT"
