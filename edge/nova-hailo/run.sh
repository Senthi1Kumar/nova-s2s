#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/scripts/setup_env.sh"
exec python3 "${ROOT}/main.py" "$@"
