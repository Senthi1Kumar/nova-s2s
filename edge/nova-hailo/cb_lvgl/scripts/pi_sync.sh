#!/usr/bin/env bash
# Copy cb_lvgl to the Pi, build both renderers there, run the tests. Run on the laptop.
set -euo pipefail
PI=${PI:-RPi5-AI-HAT+2}
DEST=${DEST:-repos/test_nova_repo/nova-s2s/edge/nova-hailo/cb_lvgl}
RENDERERS=${RENDERERS:-gpu sw}
HERE=$(cd "$(dirname "$0")/.." && pwd)
rsync -a --delete --exclude 'build*/' --exclude '.deps/' --exclude 'compile_commands.json' --exclude '*.log' \
  "$HERE/" "$PI:$DEST/"
ssh "$PI" "cd $DEST && for r in $RENDERERS; do \
  cmake -S . -B build-\$r -G Ninja -DCB_RENDER=\$r -DCMAKE_BUILD_TYPE=Release > cfg-\$r.log 2>&1 || { tail -20 cfg-\$r.log; exit 1; }; \
  ninja -C build-\$r > build-\$r.log 2>&1 || { grep -E 'error|warning' build-\$r.log | head -30; exit 1; }; \
  echo \"== \$r built\"; ctest --test-dir build-\$r --output-on-failure 2>&1 | tail -4; done"
