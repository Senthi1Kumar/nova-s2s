#!/usr/bin/env bash
# Rsync this tree to the Pi (no --delete; never .env, .venv, runtime).
set -euo pipefail
cd "$(dirname "$0")/.."
PI_HOST="${PI_HOST:-RPi5-AI-HAT+2}"
PI_PATH="${PI_PATH:-/home/cariad/repos/test_nova_repo/nova-s2s/edge/hailo-ai-box}"
ssh "$PI_HOST" "mkdir -p '$PI_PATH'"
rsync -av --exclude .venv --exclude runtime --exclude .env --exclude __pycache__ \
  --exclude .pytest_cache --exclude .ruff_cache --exclude '*.egg-info' ./ "$PI_HOST:$PI_PATH/"
