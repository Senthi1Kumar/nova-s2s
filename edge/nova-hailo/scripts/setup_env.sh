#!/usr/bin/env bash
# Activate uv venv + hailo system libs + PYTHONPATH.
# Usage:  source scripts/setup_env.sh
#
# SOURCE-SAFE: do not `set -e` and do not `exit` here. This file is sourced
# from zsh/bash; `set -e` / `exit` would kill the interactive terminal
# (looks like "it started installing and the shell vanished").
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _NOVA_SRC="${BASH_SOURCE[0]}"
elif [[ -n "${ZSH_VERSION:-}" && -n "${(%):-%x}" ]]; then
  # zsh: %x is this file when sourced
  _NOVA_SRC="${(%):-%x}"
else
  _NOVA_SRC="$0"
fi
_NOVA_ROOT="$(cd "$(dirname "$_NOVA_SRC")/.." && pwd)"

nova_hailo_setup_env() {
  local root sys_py d
  root="${1:?}"

  if [[ -z "${HAILO_APPS:-}" || ! -d "${HAILO_APPS:-/}" ]]; then
    for d in /home/cariad/code/hailo-apps "$HOME/code/hailo-apps" /opt/hailo/hailo-apps; do
      if [[ -d "$d" ]]; then
        HAILO_APPS="$d"
        break
      fi
    done
  fi
  HAILO_APPS="${HAILO_APPS:-/home/cariad/code/hailo-apps}"

  cd "$root" || return 1

  # hailo_platform = HailoRT Debian package, not PyPI.
  # uv venv (not python -m venv) but MUST pin system Python + site-packages.
  sys_py="${NOVA_HAILO_PYTHON:-/usr/bin/python3}"
  if [[ ! -x "$sys_py" ]]; then
    sys_py="$(command -v python3)"
  fi
  if [[ ! -d "$root/.venv" ]]; then
    echo "uv venv --python $sys_py --system-site-packages  (HailoRT hailo_platform)"
    echo "  (PySide6 / onnxruntime — first install can take several minutes)"
    if ! (
      cd "$root" || exit 1
      uv venv --python "$sys_py" --system-site-packages .venv || exit 1
      uv pip install -e .
    ); then
      echo "ERROR: uv venv / uv pip install -e . failed (shell stays open)." >&2
      return 1
    fi
  fi
  if [[ ! -f "$root/.venv/bin/python3" ]]; then
    echo "ERROR: venv missing at $root/.venv" >&2
    return 1
  fi

  # shellcheck disable=SC1091
  source "$root/.venv/bin/activate" || return 1

  if [[ -f "$root/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$root/.env"
    set +a
  fi

  export PYTHONPATH="${root}:${HAILO_APPS}:${PYTHONPATH:-}"
  export NOVA_HAILO_ROOT="$root"
  _nemo_lib="${root}/models/nemo_speech"
  if [[ -d "$_nemo_lib" ]]; then
    export LD_LIBRARY_PATH="${_nemo_lib}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi

  echo "nova-hailo env ready ($root)"
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
}

nova_hailo_setup_env "$_NOVA_ROOT"
