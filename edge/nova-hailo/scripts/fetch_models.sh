#!/usr/bin/env bash
# One-shot STT / VAD / TTS setup for nova-hailo.
#
#   source scripts/setup_env.sh
#   ./scripts/fetch_models.sh
#
# Downloads public STT/VAD/TTS weights + the Qwen2-1.5B Hailo HEF that
# matches this board's HailoRT firmware (hailortcli fw-control identify),
# clones/builds NVIDIA NeMo-Speech.cpp (cpu-server) and mudler/parakeet.cpp
# (libparakeet.so), and installs the .so files next to their GGUFs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NEMO_REPO="${NEMO_SPEECH_REPO:-https://github.com/NVIDIA/NeMo-Speech.cpp.git}"
NEMO_REF="${NEMO_SPEECH_REF:-2e12e2d}"   # initial OSS release (matches our ctypes ABI)
NEMO_SRC="${NEMO_SPEECH_SRC:-$ROOT/cloned/NeMo-Speech.cpp}"
NEMO_PRESET="${NEMO_SPEECH_PRESET:-cpu-server}"
PARAKEET_REPO="${PARAKEET_REPO:-https://github.com/mudler/parakeet.cpp.git}"
PARAKEET_REF="${PARAKEET_REF:-1bfbebf}"  # C API ABI >= 5 (parakeet_stt.py)
PARAKEET_SRC="${PARAKEET_SRC:-$ROOT/cloned/parakeet.cpp}"
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
(root / "models" / "parakeet").mkdir(parents=True, exist_ok=True)
(root / "models" / "piper").mkdir(parents=True, exist_ok=True)

print("STT  nvidia/nemotron-speech-streaming-en-0.6b")
hf_hub_download(
    "nvidia/nemotron-speech-streaming-en-0.6b",
    "nemotron-speech-streaming-en-0.6b.q8_0.gguf",
    local_dir=str(root / "models" / "nemo_speech"),
)

print("STT  mudler/parakeet-cpp-gguf tdt_ctc-110m-f16")
hf_hub_download(
    "mudler/parakeet-cpp-gguf",
    "tdt_ctc-110m-f16.gguf",
    local_dir=str(root / "models" / "parakeet"),
)

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

# Silero is a GitHub project, not a public HF model (HF returns 401 / repo-not-found).
# Same URL as nova_hailo/web/silero_vad.py::ONNX_URL.
VAD_DST="$ROOT/models/silero_vad.onnx"
VAD_URL="https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
if [[ -s "$VAD_DST" ]]; then
  log "VAD already at $VAD_DST"
else
  log "VAD  $VAD_URL → models/silero_vad.onnx"
  mkdir -p "$(dirname "$VAD_DST")"
  if command -v curl >/dev/null; then
    curl -fsSL -o "$VAD_DST" "$VAD_URL"
  else
    wget -q -O "$VAD_DST" "$VAD_URL"
  fi
  [[ -s "$VAD_DST" ]] || { log "ERROR: Silero VAD download empty"; exit 1; }
fi

# DTLN NS (breizhn ONNX). Same files as qt_mic_app / PiDTLN NS — not DTLN-aec.
DTLN_DIR="$ROOT/models/dtln"
mkdir -p "$DTLN_DIR"
for name in model_1.onnx model_2.onnx; do
  dst="$DTLN_DIR/$name"
  if [[ -s "$dst" ]]; then
    log "DTLN already at $dst"
    continue
  fi
  url="https://github.com/breizhn/DTLN/raw/master/pretrained_model/${name}"
  log "DTLN  $url"
  if command -v curl >/dev/null; then
    curl -fL --retry 3 --retry-delay 2 -o "$dst" "$url"
  else
    wget -q -O "$dst" "$url"
  fi
  [[ -s "$dst" ]] || { log "ERROR: DTLN $name empty"; rm -f "$dst"; exit 1; }
done

# ---------------------------------------------------------------------------
# 1b. Hailo LLM HEF — Qwen2-1.5B-Instruct (alias qwen2)
#     Zoo path is firmware-specific: a 5.1.1 HEF will not load on 5.3.0 FW.
#     Detect via `hailortcli fw-control identify` (override: HAILO_LLM_ZOO_VERSION
#     or HAILO_LLM_HEF_URL). Sidecar models/*.hef.hailort-zoo records the tag.
# ---------------------------------------------------------------------------
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
IDENTIFY_TXT=""
if command -v hailortcli >/dev/null; then
  IDENTIFY_TXT="$(hailortcli fw-control identify 2>&1 || true)"
  if [[ -n "$IDENTIFY_TXT" ]]; then
    log "hailortcli fw-control identify:"
    printf '%s\n' "$IDENTIFY_TXT" | sed 's/^/[fetch_models]   /'
  fi
else
  log "hailortcli not on PATH — HEF zoo tag from env or default 5.3.0"
fi

FETCH_META="$ROOT/models/.hailo_llm_fetch.env"
mkdir -p "$ROOT/models"
HAILO_IDENTIFY_TXT="$IDENTIFY_TXT" "$PY" > "$FETCH_META" <<'PY'
import os
import sys
from nova_hailo.hailort_fw import (
    DEFAULT_FC_HEF,
    DEFAULT_FIRMWARE,
    DEFAULT_LLM_HEF,
    HEF_NAME_RE,
    detect_firmware,
    llm_hef_url,
    zoo_tag_for_firmware,
)

identify = os.environ.get("HAILO_IDENTIFY_TXT", "")
fw = detect_firmware(identify or None)
zoo = zoo_tag_for_firmware(fw)
hef = os.environ.get("HAILO_LLM_HEF_NAME") or DEFAULT_LLM_HEF
if not HEF_NAME_RE.match(hef):
    print(f"ERROR: invalid HAILO_LLM_HEF_NAME={hef!r}", file=sys.stderr)
    sys.exit(1)
url = os.environ.get("HAILO_LLM_HEF_URL") or llm_hef_url(zoo, hef)
if "\n" in url or " " in url.strip():
    print("ERROR: HAILO_LLM_HEF_URL must be a single URL token", file=sys.stderr)
    sys.exit(1)
fc_url = llm_hef_url(zoo, DEFAULT_FC_HEF)
# KEY=value only; fetch_models.sh reads known keys, never eval.
print(f"HAILO_FW={fw}")
print(f"HAILO_ZOO_TAG={zoo}")
print(f"HEF_NAME={hef}")
print(f"HEF_URL={url}")
print(f"FC_HEF_NAME={DEFAULT_FC_HEF}")
print(f"FC_HEF_URL={fc_url}")
if not identify and fw == DEFAULT_FIRMWARE and not (
    os.environ.get("HAILO_LLM_ZOO_VERSION") or os.environ.get("HAILO_RT_VERSION")
):
    print(
        f"WARN default firmware {DEFAULT_FIRMWARE} "
        "(no hailortcli identify; set HAILO_LLM_ZOO_VERSION to pin)",
        file=sys.stderr,
    )
PY
HAILO_FW="" HAILO_ZOO_TAG="" HEF_NAME="" HEF_URL="" FC_HEF_NAME="" FC_HEF_URL=""
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    HAILO_FW=*) HAILO_FW="${line#HAILO_FW=}" ;;
    HAILO_ZOO_TAG=*) HAILO_ZOO_TAG="${line#HAILO_ZOO_TAG=}" ;;
    HEF_NAME=*) HEF_NAME="${line#HEF_NAME=}" ;;
    HEF_URL=*) HEF_URL="${line#HEF_URL=}" ;;
    FC_HEF_NAME=*) FC_HEF_NAME="${line#FC_HEF_NAME=}" ;;
    FC_HEF_URL=*) FC_HEF_URL="${line#FC_HEF_URL=}" ;;
  esac
done < "$FETCH_META"
[[ -n "$HEF_NAME" && -n "$HEF_URL" && -n "$HAILO_ZOO_TAG" ]] || {
  log "ERROR: failed to resolve Hailo LLM zoo URL (see $FETCH_META)"
  exit 1
}
HEF_DST="$ROOT/models/$HEF_NAME"
HEF_STAMP="${HEF_DST}.hailort-zoo"
log "firmware ${HAILO_FW} → zoo ${HAILO_ZOO_TAG}"

download_hef() {
  local url="$1" dest="$2" label="$3"
  mkdir -p "$(dirname "$dest")"
  local tmp="${dest}.partial"
  if command -v curl >/dev/null; then
    curl -fL --retry 3 --retry-delay 2 -o "$tmp" "$url"
  else
    wget -q -O "$tmp" "$url"
  fi
  [[ -s "$tmp" ]] || { log "ERROR: $label download empty ($url)"; rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$dest"
}

need_hef=1
if [[ "${FORCE_LLM_HEF:-0}" == 1 ]]; then
  log "FORCE_LLM_HEF=1 — re-download ${HEF_NAME} for ${HAILO_ZOO_TAG}"
  need_hef=1
elif [[ -s "$HEF_DST" && -s "$HEF_STAMP" ]] && [[ "$(cat "$HEF_STAMP")" == "$HAILO_ZOO_TAG" ]]; then
  log "LLM already at $HEF_DST (${HAILO_ZOO_TAG})"
  need_hef=0
elif [[ -s "$HEF_DST" && "${HAILO_KEEP_EXISTING_HEF:-0}" == 1 ]]; then
  log "LLM keeping existing $HEF_DST (HAILO_KEEP_EXISTING_HEF=1; wanted ${HAILO_ZOO_TAG})"
  printf '%s\n' "$HAILO_ZOO_TAG" > "$HEF_STAMP"
  need_hef=0
elif [[ -s "$HEF_DST" ]]; then
  old="$(cat "$HEF_STAMP" 2>/dev/null || echo unstamped)"
  log "LLM HEF zoo mismatch (have ${old}, want ${HAILO_ZOO_TAG}) — re-download"
fi

if [[ "$need_hef" -eq 1 ]]; then
  # Do not silently reuse /usr/local Hailo-apps HEFs — those are often 5.1.1.
  log "LLM  $HEF_URL → models/${HEF_NAME}  (~1.5 GB, firmware ${HAILO_FW}, zoo ${HAILO_ZOO_TAG})"
  download_hef "$HEF_URL" "$HEF_DST" "LLM HEF"
  printf '%s\n' "$HAILO_ZOO_TAG" > "$HEF_STAMP"
fi

# Function-calling HEF: 5.2+ zoo. Default on for 5.3 so qwen2-fc is on disk;
# chat still uses Instruct unless model.llm_hef: qwen2-fc.
if [[ "${HAILO_FETCH_FC_HEF:-}" == "0" ]]; then
  log "skip FC HEF (HAILO_FETCH_FC_HEF=0)"
elif [[ "${HAILO_FETCH_FC_HEF:-1}" == 1 ]] && [[ "${HAILO_ZOO_TAG:-}" == v5.2.* || "${HAILO_ZOO_TAG:-}" == v5.3.* || "${HAILO_ZOO_TAG:-}" == v5.4.* ]]; then
  HAILO_FETCH_FC_HEF=1
fi
if [[ "${HAILO_FETCH_FC_HEF:-0}" == 1 ]]; then
  FC_DST="$ROOT/models/$FC_HEF_NAME"
  FC_STAMP="${FC_DST}.hailort-zoo"
  if [[ -s "$FC_DST" && -s "$FC_STAMP" && "$(cat "$FC_STAMP")" == "$HAILO_ZOO_TAG" && "${FORCE_LLM_HEF:-0}" != 1 ]]; then
    log "FC LLM already at $FC_DST (${HAILO_ZOO_TAG})"
  else
    log "FC LLM  $FC_HEF_URL → models/${FC_HEF_NAME}  (optional, HailoRT >= 5.2 tools HEF)"
    if download_hef "$FC_HEF_URL" "$FC_DST" "FC LLM HEF"; then
      printf '%s\n' "$HAILO_ZOO_TAG" > "$FC_STAMP"
    else
      log "FC HEF skipped (not on ${HAILO_ZOO_TAG} zoo — Instruct HEF is the demo default)"
    fi
  fi
fi

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
  # cpu-server writes shared libs to build/<preset>/bin/ (CMAKE_LIBRARY_OUTPUT_DIRECTORY).
  # SOVERSION=1 → libnemo_speech_asr_c.so.1. ctypes wants libnemo_speech_asr_c.so.
  # _c is linked to libnemo_speech_asr.so via $ORIGIN — copy the pair.
  local dest bindir
  dest="$(dirname "$LIB_DST")"
  mkdir -p "$dest"
  bindir="$(find "$NEMO_SRC/build" -type d -name bin 2>/dev/null | head -1 || true)"
  # Shared libs live under build/<preset>/bin AND ggml's own cmake dirs.
  find "$NEMO_SRC/build" \( \
      -name 'libnemo_speech_asr*.so' -o -name 'libnemo_speech_asr*.so.*' \
      -o -name 'libggml.so' -o -name 'libggml.so.*' \
      -o -name 'libggml-base.so*' -o -name 'libggml-cpu.so*' \
      -o -name 'libggml-*.so*' \
    \) \( -type f -o -type l \) -exec cp -aL {} "$dest/" \;
  log "native libs in $dest:"
  ls -l "$dest"/lib*.so* 2>/dev/null | sed 's/^/[fetch_models]   /' || true
  if [[ ! -e "$LIB_DST" ]]; then
    local ver
    ver="$(ls -1 "$dest"/libnemo_speech_asr_c.so.* 2>/dev/null | head -1 || true)"
    if [[ -n "$ver" ]]; then
      ln -sfn "$(basename "$ver")" "$LIB_DST"
    fi
  fi
  if [[ ! -e "$LIB_DST" ]]; then
    log "ERROR: libnemo_speech_asr_c.so* not under $NEMO_SRC/build"
    find "$NEMO_SRC/build" -name 'libnemo_speech*' 2>/dev/null | head -20 || true
    return 1
  fi
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
# 2a. parakeet.cpp — libparakeet.so (C API ABI >= 5) + GGUF already in models/
# ---------------------------------------------------------------------------
PK_LIB_DST="$ROOT/models/parakeet/libparakeet.so"

clone_parakeet() {
  mkdir -p "$(dirname "$PARAKEET_SRC")"
  if [[ ! -d "$PARAKEET_SRC/.git" ]]; then
    log "clone $PARAKEET_REPO → $PARAKEET_SRC"
    git clone --filter=blob:none "$PARAKEET_REPO" "$PARAKEET_SRC"
  fi
  if git -C "$PARAKEET_SRC" rev-parse --verify "${PARAKEET_REF}^{commit}" >/dev/null 2>&1; then
    git -C "$PARAKEET_SRC" checkout --detach "$PARAKEET_REF"
  else
    git -C "$PARAKEET_SRC" fetch --depth 1 origin "$PARAKEET_REF"
    git -C "$PARAKEET_SRC" checkout --detach FETCH_HEAD
  fi
  log "parakeet.cpp @ $(git -C "$PARAKEET_SRC" rev-parse --short HEAD)"
  git -C "$PARAKEET_SRC" submodule update --init --depth 1 third_party/ggml
}

build_parakeet() {
  log "configure + build parakeet shared lib (CPU C ABI)"
  # $ORIGIN so libparakeet.so finds libggml*.so after copy to models/parakeet/.
  cmake -S "$PARAKEET_SRC" -B "$PARAKEET_SRC/build-shared" \
    -DCMAKE_BUILD_TYPE=Release \
    -DPARAKEET_SHARED=ON \
    -DPARAKEET_BUILD_CLI=OFF \
    -DPARAKEET_BUILD_SERVER=OFF \
    -DPARAKEET_BUILD_TESTS=OFF \
    -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DCMAKE_INSTALL_RPATH='$ORIGIN'
  cmake --build "$PARAKEET_SRC/build-shared" --config Release -j"$(nproc)"
}

install_parakeet_lib() {
  local dest="$ROOT/models/parakeet"
  mkdir -p "$dest"
  find "$PARAKEET_SRC/build-shared" \( \
      -name 'libparakeet.so' -o -name 'libparakeet.so.*' \
      -o -name 'libggml.so' -o -name 'libggml.so.*' \
      -o -name 'libggml-base.so*' -o -name 'libggml-cpu.so*' \
      -o -name 'libggml-*.so*' \
    \) \( -type f -o -type l \) -exec cp -aL {} "$dest/" \;
  log "parakeet libs in $dest:"
  ls -l "$dest"/lib*.so* 2>/dev/null | sed 's/^/[fetch_models]   /' || true
  if [[ ! -e "$PK_LIB_DST" ]]; then
    local ver
    ver="$(ls -1 "$dest"/libparakeet.so.* 2>/dev/null | head -1 || true)"
    if [[ -n "$ver" ]]; then
      ln -sfn "$(basename "$ver")" "$PK_LIB_DST"
    fi
  fi
  if [[ ! -e "$PK_LIB_DST" ]]; then
    log "ERROR: libparakeet.so* not under $PARAKEET_SRC/build-shared"
    find "$PARAKEET_SRC/build-shared" -name 'libparakeet*' 2>/dev/null | head -20 || true
    return 1
  fi
  if command -v patchelf >/dev/null; then
    find "$dest" -maxdepth 1 -name 'lib*.so*' -type f -exec patchelf --set-rpath '$ORIGIN' {} \;
  fi
  log "installed $PK_LIB_DST ($(du -h "$PK_LIB_DST" | awk '{print $1}'))"
}

if [[ -f "$PK_LIB_DST" && "${FORCE_PARAKEET_BUILD:-0}" != 1 ]]; then
  log "lib already at $PK_LIB_DST (FORCE_PARAKEET_BUILD=1 to rebuild)"
else
  need_tools
  clone_parakeet
  build_parakeet
  install_parakeet_lib
fi

# ---------------------------------------------------------------------------
# 2b. Native Hailo LLM C++ backend — must be compiled on THIS Pi against the
#     installed HailoRT (5.1.1 vs 5.3 generate API). Not shipped in git.
# ---------------------------------------------------------------------------
if [[ "${SKIP_LLM_CPP:-0}" == 1 ]]; then
  log "skip hailo_llm_cpp (SKIP_LLM_CPP=1)"
elif command -v hailortcli >/dev/null 2>&1; then
  log "building hailo_llm_cpp for firmware ${HAILO_FW:-?} (HailoRT on this board)"
  bash "$ROOT/scripts/build_hailo_llm_cpp.sh"
  if ! find "$ROOT/nova_hailo/backends" -maxdepth 1 -name 'hailo_llm_cpp*.so' | grep -q .; then
    log "ERROR: hailo_llm_cpp*.so missing after build"
    exit 1
  fi
else
  log "skip hailo_llm_cpp (no hailortcli — this is not the Hailo Pi)"
fi

# ---------------------------------------------------------------------------
# 3. Layout check
# ---------------------------------------------------------------------------
ok=1
for rel in \
  models/nemo_speech/nemotron-speech-streaming-en-0.6b.q8_0.gguf \
  models/nemo_speech/libnemo_speech_asr_c.so \
  models/parakeet/tdt_ctc-110m-f16.gguf \
  models/parakeet/libparakeet.so \
  models/silero_vad.onnx \
  models/Inflect-Nano-v2-ONNX \
  models/piper/en_US-amy-low.onnx \
  models/Qwen2-1.5B-Instruct.hef
do
  if [[ -e "$ROOT/$rel" ]]; then
    log "OK  $rel"
  else
    log "MISSING  $rel"
    ok=0
  fi
done

if [[ "$ok" -eq 1 ]]; then
  log "STT + VAD + TTS + LLM HEF ready under models/ (LLM zoo ${HAILO_ZOO_TAG:-?} fw ${HAILO_FW:-?})"
else
  log "some artifacts missing — see lines above"
  exit 1
fi
