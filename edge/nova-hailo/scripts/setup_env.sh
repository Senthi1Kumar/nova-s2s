#!/usr/bin/env bash
# Activate uv venv + hailo system libs + PYTHONPATH for e2e
# Usage: source scripts/setup_env.sh   OR   bash scripts/setup_env.sh
set -euo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _SRC="${BASH_SOURCE[0]}"
else
  _SRC="$0"
fi
ROOT="$(cd "$(dirname "$_SRC")/.." && pwd)"
HAILO_APPS="${HAILO_APPS:-/home/cariad/code/hailo-apps}"

cd "$ROOT"
if [[ ! -d "$ROOT/.venv" ]]; then
  echo "Creating uv venv with system-site-packages (for hailo_platform)..."
  (cd "$ROOT" && uv venv --system-site-packages .venv && uv pip install -e .)
fi
# reinstall if missing package marker
if [[ ! -f "$ROOT/.venv/bin/python3" ]]; then
  echo "ERROR: venv missing at $ROOT/.venv"
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

# Load local secrets if present (never required for chat-only)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export PYTHONPATH="${ROOT}:${HAILO_APPS}:${PYTHONPATH:-}"
export NOVA_HAILO_ROOT="$ROOT"

echo "nova-hailo env ready ($ROOT)"
echo "  python: $(command -v python3)"
python3 - <<'PY'
import sys
print("  hailo check...", end=" ")
try:
    import hailo_platform
    print("OK")
except Exception as e:
    print("FAIL", e)
try:
    import piper, sounddevice, webrtcvad, nova_hailo
    print("  deps OK")
except Exception as e:
    print("  deps FAIL", e)
PY
