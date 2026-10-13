#!/usr/bin/env bash
# Runs a command with MAPBOX_API_KEY loaded from nova-hailo's .env (see mapbox_env.sh).
#   scripts/with_mapbox.sh build-sw/map_probe --style mapbox://styles/mapbox/dark-v11
set -euo pipefail
source "$(dirname "$0")/mapbox_env.sh"
cb_load_mapbox_key
exec "$@"
