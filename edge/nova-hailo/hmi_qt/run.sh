#!/usr/bin/env bash
# Nova HMI — Qt/QML against the live nova-hailo backend.
# Prefers the nova-hailo venv (PySide6 from pyproject.toml). Separate
# hmi_qt/.venv is only a fallback if the parent env is missing.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
elif [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "HMI: no parent .venv — uv venv --system-site-packages (run source ../scripts/setup_env.sh normally)"
  SYS_PY="${NOVA_HAILO_PYTHON:-/usr/bin/python3}"
  [[ -x "$SYS_PY" ]] || SYS_PY="$(command -v python3)"
  uv venv --python "$SYS_PY" --system-site-packages .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -r requirements.txt
fi
WS="${NOVA_HAILO_WS_URL:-ws://127.0.0.1:8766/v1/realtime}"
export NOVA_HAILO_WS_URL="$WS"
if [[ -z "${DISPLAY:-}" && -S /tmp/.X11-unix/X0 ]]; then
  export DISPLAY=:0
  export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
  echo "No DISPLAY in this shell — using DISPLAY=:0 (device screen)."
fi
echo "Nova HMI → $WS  DISPLAY=${DISPLAY:-unset}"
echo "Close the browser voice tab first (backend allows one session)."
echo "Esc quits. Do not use --kiosk over SSH unless you can reach the HDMI."
exec python main.py --ws "$WS" --mic "$@"
