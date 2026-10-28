#!/usr/bin/env bash
# Run aibox_studio on the Pi's desktop session (labwc) from an SSH shell.
#   scripts/run_on_pi.sh gpu|sw [app args...]
# gpu: SDL's X11 driver on Xwayland (LVGL v9.6's SDL EGL path supports X11 only); sw: SDL's native Wayland driver.
set -euo pipefail
R=${1:?usage: run_on_pi.sh gpu|sw [args...]}
shift
cd "$(dirname "$0")/.."
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 DISPLAY=:0
if [ "$R" = gpu ]; then export SDL_VIDEODRIVER=x11; else export SDL_VIDEODRIVER=wayland; fi
exec "./build-$R/aibox_studio" "$@"
