#!/usr/bin/env bash
# Copy studio_lvgl to the Pi (no --delete) and build there. Run on the laptop.
#   scripts/pi_sync.sh [gpu|sw ...]
set -euo pipefail
PI=${PI:-RPi5-AI-HAT+2}
DEST=${DEST:-/home/cariad/repos/test_nova_repo/nova-s2s/edge/hailo-ai-box/studio_lvgl}
RENDERERS=${*:-gpu}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SSH="ssh -o ClearAllForwardings=yes"
rsync -a -e "$SSH" --exclude 'build*/' --exclude 'compile_commands.json' --exclude '*.log' "$HERE/" "$PI:$DEST/"
$SSH "$PI" "cd $DEST && for r in $RENDERERS; do \
  cmake -S . -B build-\$r -G Ninja -DSTUDIO_RENDER=\$r -DCMAKE_BUILD_TYPE=Release ${STUDIO_CMAKE_ARGS:-} > cfg-\$r.log 2>&1 || { tail -20 cfg-\$r.log; exit 1; }; \
  ninja -C build-\$r -j2 > build-\$r.log 2>&1 || { grep -E 'error|warning' build-\$r.log | head -30; exit 1; }; \
  echo \"== \$r built\"; done"
