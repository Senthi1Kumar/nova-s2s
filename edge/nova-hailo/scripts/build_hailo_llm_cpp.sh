#!/usr/bin/env bash
# Build the opt-in native LLM backend (csrc/hailo_llm_cpp.cpp). Pi-only --
# needs HailoRT's CMake package and pybind11; not part of the normal
# `pip install -e .` flow, matching how hailo_platform itself is a
# system-site-package, not a pip dependency.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/setup_env.sh" 2>/dev/null || true
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

python3 -c "import pybind11" 2>/dev/null || {
  UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$UV_BIN" pip install pybind11
}

BUILD_DIR="$ROOT/csrc/build"
cmake -S "$ROOT/csrc" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release \
  -Dpybind11_DIR="$(python3 -c 'import pybind11; print(pybind11.get_cmake_dir())')"
cmake --build "$BUILD_DIR" --config Release -j"$(nproc)"

# Drop the built extension next to backends/ so `import hailo_llm_cpp` works
# without further path wrangling.
find "$BUILD_DIR" -maxdepth 1 -name "hailo_llm_cpp*.so" -exec cp {} "$ROOT/nova_hailo/backends/" \;
echo "Built: $(find "$ROOT/nova_hailo/backends" -maxdepth 1 -name 'hailo_llm_cpp*.so')"
