#!/usr/bin/env bash
# One-shot STT / VAD / TTS setup for nova-hailo.
#
#   source scripts/setup_env.sh
#   ./scripts/fetch_models.sh
#
# Downloads public weights into models/, clones/builds NVIDIA NeMo-Speech.cpp
# (cpu-server), and installs libnemo_speech_asr_c.so next to the GGUF.
# No USB / manual .so copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NEMO_REPO="${NEMO_SPEECH_REPO:-https://github.com/NVIDIA/NeMo-Speech.cpp.git}"
NEMO_REF="${NEMO_SPEECH_REF:-2e12e2d}"   # initial OSS release (matches our ctypes ABI)
NEMO_SRC="${NEMO_SPEECH_SRC:-$ROOT/cloned/NeMo-Speech.cpp}"
NEMO_PRESET="${NEMO_SPEECH_PRESET:-cpu-server}"
PY="${ROOT}/.venv/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3)"

log() { printf '[fetch_models] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Public weights → models/
# ---------------------------------------------------------------------------
log "downloading STT / VAD / TTS weights into ${ROOT}/models/"
"$PY" - <<'PY'
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

root = Path(".").resolve()
(root / "models" / "nemo_speech").mkdir(parents=True, exist_ok=True)
(root / "models" / "piper").mkdir(parents=True, exist_ok=True)

print("STT  nvidia/nemotron-speech-streaming-en-0.6b")
hf_hub_download(
    "nvidia/nemotron-speech-streaming-en-0.6b",
    "nemotron-speech-streaming-en-0.6b.q8_0.gguf",
    local_dir=str(root / "models" / "nemo_speech"),
)

print("VAD  snakers4/silero-vad → models/silero_vad.onnx")
p = hf_hub_download("snakers4/silero-vad", "src/silero_vad/data/silero_vad.onnx")
dest = root / "models" / "silero_vad.onnx"
dest.write_bytes(Path(p).read_bytes())

print("TTS  owensong/Inflect-Nano-v2-ONNX → models/Inflect-Nano-v2-ONNX/")
snapshot_download(
    "owensong/Inflect-Nano-v2-ONNX",
    local_dir=str(root / "models" / "Inflect-Nano-v2-ONNX"),
)

print("TTS  rhasspy/piper-voices amy-low → models/piper/")
for name in ("en_US-amy-low.onnx", "en_US-amy-low.onnx.json"):
    hf_hub_download(
        "rhasspy/piper-voices",
        f"en/en_US/amy/low/{name}",
        local_dir=str(root / "models" / "piper"),
    )
    src = root / "models" / "piper" / "en" / "en_US" / "amy" / "low" / name
    if src.is_file():
        (root / "models" / "piper" / name).write_bytes(src.read_bytes())

print("weights ok")
PY

# ---------------------------------------------------------------------------
# 2. Build NeMo-Speech.cpp (CPU) and install libnemo_speech_asr_c.so
# ---------------------------------------------------------------------------
LIB_DST="$ROOT/models/nemo_speech/libnemo_speech_asr_c.so"

need_tools() {
  local missing=()
  command -v git >/dev/null || missing+=(git)
  command -v ninja >/dev/null || missing+=(ninja-build)
  command -v g++ >/dev/null || missing+=(build-essential)
  command -v pkg-config >/dev/null || missing+=(pkg-config)
  if ((${#missing[@]})); then
    log "installing build tools: ${missing[*]}"
    sudo apt-get update -y
    sudo apt-get install -y "${missing[@]}" cmake || sudo apt-get install -y "${missing[@]}"
  fi
  # NeMo-Speech.cpp wants CMake ≥ 3.26; Pi apt is often older.
  local ver
  ver="$(cmake --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ -z "$ver" ]] || awk -v v="$ver" 'BEGIN { split(v,a,"."); exit !((a[1]<3) || (a[1]==3 && a[2]<26)) }'; then
    log "CMake ${ver:-missing} < 3.26 — installing via pip into the venv"
    "$PY" -m pip install -q 'cmake>=3.26'
    export PATH="$(dirname "$PY"):$PATH"
  fi
}

clone_nemo() {
  mkdir -p "$(dirname "$NEMO_SRC")"
  if [[ ! -d "$NEMO_SRC/.git" ]]; then
    log "clone $NEMO_REPO → $NEMO_SRC"
    git clone --filter=blob:none "$NEMO_REPO" "$NEMO_SRC"
  fi
  if git -C "$NEMO_SRC" rev-parse --verify "${NEMO_REF}^{commit}" >/dev/null 2>&1; then
    git -C "$NEMO_SRC" checkout --detach "$NEMO_REF"
  else
    git -C "$NEMO_SRC" fetch --depth 1 origin "$NEMO_REF"
    git -C "$NEMO_SRC" checkout --detach FETCH_HEAD
  fi
  log "NeMo-Speech.cpp @ $(git -C "$NEMO_SRC" rev-parse --short HEAD)"
  git -C "$NEMO_SRC" submodule update --init --depth 1 ggml
  if [[ "$NEMO_PRESET" == *server* ]]; then
    git -C "$NEMO_SRC" submodule update --init --depth 1 third_party/cpp-httplib
  fi
}

build_nemo() {
  log "configure + build preset=$NEMO_PRESET (CPU ASR C ABI)"
  (
    cd "$NEMO_SRC"
    bash scripts/configure.sh "$NEMO_PRESET"
    cmake --build --preset "$NEMO_PRESET" -j"$(nproc)"
  )
}

install_nemo_lib() {
  local found
  found="$(find "$NEMO_SRC/build" -name 'libnemo_speech_asr_c.so' -type f 2>/dev/null | head -1 || true)"
  if [[ -z "$found" ]]; then
    log "ERROR: libnemo_speech_asr_c.so not produced under $NEMO_SRC/build"
    return 1
  fi
  mkdir -p "$(dirname "$LIB_DST")"
  cp -f "$found" "$LIB_DST"
  log "installed $LIB_DST ($(du -h "$LIB_DST" | awk '{print $1}'))"
}

if [[ -f "$LIB_DST" && "${FORCE_NEMO_BUILD:-0}" != 1 ]]; then
  log "lib already at $LIB_DST (FORCE_NEMO_BUILD=1 to rebuild)"
else
  need_tools
  clone_nemo
  build_nemo
  install_nemo_lib
fi

# ---------------------------------------------------------------------------
# 3. Layout check
# ---------------------------------------------------------------------------
ok=1
for rel in \
  models/nemo_speech/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  models/nemo_speech/libnemo_speech_asr_c.so \
  models/silero_vad.onnx \
  models/Inflect-Nano-v2-ONNX \
  models/piper/en_US-amy-low.onnx
do
  if [[ -e "$ROOT/$rel" ]]; then
    log "OK  $rel"
  else
    log "MISSING  $rel"
    ok=0
  fi
done

log "LLM HEF (qwen2) comes from hailo-apps, not this script."
if [[ "$ok" -eq 1 ]]; then
  log "STT + VAD + TTS ready under models/"
else
  log "some artifacts missing — see lines above"
  exit 1
fi
