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
if [[ -z "${HAILO_APPS:-}" || ! -d "${HAILO_APPS:-/}" ]]; then
  for d in /home/cariad/code/hailo-apps "$HOME/code/hailo-apps" /opt/hailo/hailo-apps; do
    if [[ -d "$d" ]]; then
      HAILO_APPS="$d"
      break
    fi
  done
fi
HAILO_APPS="${HAILO_APPS:-/home/cariad/code/hailo-apps}"

cd "$ROOT"
# hailo_platform is the HailoRT Debian/system package (h10-hailort), not PyPI.
# uv's downloaded CPython (e.g. 3.12 under ~/.local/share/uv/python) cannot
# see it. Always bind the venv to system /usr/bin/python3.
SYS_PY="${NOVA_HAILO_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$SYS_PY" ]]; then
  SYS_PY="$(command -v python3)"
fi
if [[ ! -d "$ROOT/.venv" ]]; then
  echo "uv venv --python $SYS_PY --system-site-packages  (HailoRT hailo_platform)"
  (cd "$ROOT" && uv venv --python "$SYS_PY" --system-site-packages .venv && uv pip install -e .)
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
print("  hailo check...", end=" ")
try:
    import hailo_platform
    print("OK", getattr(hailo_platform, "__file__", ""))
except Exception as e:
    print("FAIL", e)
    print("  hailo_platform is the HailoRT system package, not pip.")
    print("  Recreate the venv on system Python:")
    print("    rm -rf .venv && source scripts/setup_env.sh")
    print("  Confirm the wheel/deb is installed:")
    print("    /usr/bin/python3 -c 'import hailo_platform; print(hailo_platform.__file__)'")
    print("    dpkg -l | grep -iE 'hailo|h10-hailort'")
try:
    import piper, sounddevice, webrtcvad, nova_hailo
    print("  deps OK")
except Exception as e:
    print("  deps FAIL", e)
try:
    import PySide6
    print("  hmi PySide6 OK")
except Exception as e:
    print("  hmi PySide6 FAIL", e, "(uv pip install -e .)")
PY
