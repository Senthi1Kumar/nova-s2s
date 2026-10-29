#!/usr/bin/env bash
# Interactive Raspberry Pi 5 + Hailo-10H setup. Run ON the Pi.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
EXPECTED_HAILO=${EXPECTED_HAILO:-5.3.0}
EXPECTED_RSID_SDK=${EXPECTED_RSID_SDK:-3.6.1}
EXPECTED_RSID_FW=${EXPECTED_RSID_FW:-9.16.1.1}
RSID_REPO=${AIBOX_REALSENSE_REPO:-$HOME/repos/RealSenseID}
RSID_GIT_REF=${RSID_GIT_REF:-v3.6.1}
CHECK_ONLY=0
ASSUME_YES=0
SKIP_SYSTEM=0
SKIP_MODELS=0
SKIP_REALSENSE=0
SKIP_TESTS=0
NO_PROGRESS=0
FIRMWARE_IMAGE=""
FAILURES=0
REBOOT_REQUIRED=0
SUDO_KEEPALIVE=""
OLLAMA_PULL_PID=""
OLLAMA_TEST_PID=""

usage() {
  cat <<'EOF'
Usage: scripts/pi_setup.sh [options]

  --check                 Check everything; make no changes.
  -y, --yes               Accept safe installs/builds. Firmware still needs an image.
  --skip-system           Skip apt/system-library installation.
  --skip-models           Skip model downloads and hailo-ollama model checks.
  --skip-realsense        Skip RealSense ID SDK, firmware and USB checks.
  --skip-tests            Skip the regression tests and the on-device model check.
  --no-progress           Do not draw the bottom progress bar.
  --firmware-image PATH   Signed F455 image to use if the OPFW version differs.
  -h, --help              Show this help.

Environment overrides: EXPECTED_HAILO, EXPECTED_RSID_SDK, EXPECTED_RSID_FW,
AIBOX_REALSENSE_REPO, RSID_GIT_REF, AIBOX_HEF_DIR, AIBOX_WHISPER_ZOO, XDG_DATA_HOME.
EOF
}

while (($#)); do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --skip-system) SKIP_SYSTEM=1 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --skip-realsense) SKIP_REALSENSE=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --no-progress) NO_PROGRESS=1 ;;
    --firmware-image)
      [[ $# -ge 2 ]] || { echo "--firmware-image needs a path" >&2; exit 2; }
      FIRMWARE_IMAGE=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Stages in run order, with a typical first-run duration in seconds. The durations only drive the
# progress bar's ETA; an already-installed stage finishes in a second or two.
declare -A STAGE_SECONDS=(
  [platform]=2 [system]=90 [venv]=90 [hailo]=5 [ollama]=240 [hefs]=90 [models]=1200 [videos]=2
  [realsense]=300 [peripherals]=15 [lvgl]=120 [tests]=120 [device]=240 [apikey]=2 [setupenv]=1 [finish]=1
)
PLAN=(platform system venv hailo ollama hefs models videos)
((SKIP_REALSENSE)) || PLAN+=(realsense peripherals)
PLAN+=(lvgl)
((SKIP_TESTS)) || PLAN+=(tests device)
PLAN+=(apikey)
((CHECK_ONLY)) || PLAN+=(setupenv)
PLAN+=(finish)
STAGE_INDEX=0
PROGRESS_STATE=""
PROGRESS_PID=""
TTY_FD=""
STDOUT_TTY=0
[[ -t 1 ]] && STDOUT_TTY=1

fmt_time() { printf '%02d:%02d' $(($1 / 60)) $(($1 % 60)); }
draw_progress() {
  local start=$1 index=$2 stage_start=$3 title=$4 rows=$5 cols=$6 now i w elapsed total=0 done_s=0 left=0
  local -a weights
  read -r -a weights <<<"$7"
  now=$(date +%s)
  for i in "${!weights[@]}"; do
    w=${weights[$i]}
    total=$((total + w))
    if ((i < index)); then
      done_s=$((done_s + w))
    elif ((i == index)); then
      elapsed=$((now - stage_start))
      ((elapsed < w)) || elapsed=$w
      done_s=$((done_s + elapsed))
      left=$((left + w - elapsed))
    else
      left=$((left + w))
    fi
  done
  local pct=$((100 * done_s / total)) width filled bar text
  text=$(printf ' %3d%%  %d/%d %s  ·  %s elapsed  ·  ETA ≤ %s ' "$pct" "$((index + 1))" "${#weights[@]}" \
    "$title" "$(fmt_time $((now - start)))" "$(fmt_time "$left")")
  width=$((cols - ${#text} - 2))
  ((width >= 10)) || width=10
  filled=$((width * pct / 100))
  bar=$(printf '%*s' "$filled" '' | tr ' ' '#')$(printf '%*s' $((width - filled)) '' | tr ' ' '-')
  printf '\0337\033[%d;1H\033[2K\033[1;36m[%s]\033[0m%s\0338' "$rows" "$bar" "${text:0:$((cols - width - 2))}"
}
progress_start() {
  ((NO_PROGRESS == 0 && STDOUT_TTY)) && [[ "${TERM:-dumb}" != dumb ]] || return 0
  exec {TTY_FD}>/dev/tty || return 0
  local stage_weights="" key size rows
  for key in "${PLAN[@]}"; do stage_weights+="${STAGE_SECONDS[$key]} "; done
  PROGRESS_STATE=$(mktemp)
  size=$(stty size </dev/tty 2>/dev/null || echo "24 80")
  rows=${size%% *}
  # Reserve the bottom line: everything else scrolls above the bar.
  printf '\n\033[A\0337\033[1;%dr\0338' $((rows - 1)) >&"$TTY_FD"
  (
    trap 'exit 0' TERM
    start=$(date +%s) last_rows=$rows
    while :; do
      size=$(stty size </dev/tty 2>/dev/null || echo "24 80")
      rows=${size%% *}
      if ((rows != last_rows)); then
        printf '\0337\033[1;%dr\0338' $((rows - 1)) >&"$TTY_FD"
        last_rows=$rows
      fi
      if read -r index stage_start title <"$PROGRESS_STATE" 2>/dev/null && [[ -n "${index:-}" ]]; then
        draw_progress "$start" "$index" "$stage_start" "$title" "$rows" "${size##* }" "$stage_weights" >&"$TTY_FD"
      fi
      sleep 1
    done
  ) &
  PROGRESS_PID=$!
}
progress_stop() {
  [[ -n "$PROGRESS_PID" ]] || return 0
  kill "$PROGRESS_PID" 2>/dev/null || true
  wait "$PROGRESS_PID" 2>/dev/null || true
  local size rows
  size=$(stty size </dev/tty 2>/dev/null || echo "24 80")
  rows=${size%% *}
  printf '\0337\033[r\033[%d;1H\033[2K\0338' "$rows" >&"$TTY_FD"
  rm -f "$PROGRESS_STATE"
  PROGRESS_PID=""
}
step() {
  local key=$1 title=$2 i
  for i in "${!PLAN[@]}"; do
    [[ "${PLAN[$i]}" == "$key" ]] && STAGE_INDEX=$i
  done
  printf '\n\033[1;34m━━ [%d/%d] %s\033[0m\n' "$((STAGE_INDEX + 1))" "${#PLAN[@]}" "$title"
  if [[ -n "$PROGRESS_STATE" ]]; then
    printf '%s %s %s\n' "$STAGE_INDEX" "$(date +%s)" "$title" >"$PROGRESS_STATE.tmp"
    mv "$PROGRESS_STATE.tmp" "$PROGRESS_STATE"
  fi
}
ok() { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
confirm() {
  ((ASSUME_YES)) && return 0
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}
stop_if_failed() {
  ((FAILURES == 0)) || { printf '\nSetup cannot continue with %d failed required check(s).\n' "$FAILURES" >&2; exit 1; }
}
need_sudo() {
  ((CHECK_ONLY)) && return 0
  ((EUID == 0)) && return 0
  sudo -v
  if [[ -z "$SUDO_KEEPALIVE" ]]; then
    (while true; do sudo -n true; sleep 45; done) &
    SUDO_KEEPALIVE=$!
  fi
}
root_run() {
  if ((EUID == 0)); then "$@"; else sudo "$@"; fi
}
cleanup() {
  progress_stop
  [[ -z "$SUDO_KEEPALIVE" ]] || kill "$SUDO_KEEPALIVE" 2>/dev/null || true
  [[ -z "$OLLAMA_PULL_PID" ]] || kill "$OLLAMA_PULL_PID" 2>/dev/null || true
  [[ -z "$OLLAMA_TEST_PID" ]] || kill "$OLLAMA_TEST_PID" 2>/dev/null || true
  ((REBOOT_REQUIRED == 0)) || warn "Reboot is required for the USB power change."
}
trap cleanup EXIT

if ((CHECK_ONLY == 0)); then
  mkdir -p runtime/logs runtime/downloads
  SETUP_LOG="runtime/logs/setup-$(date +%Y%m%d-%H%M%S).log"
  touch "$SETUP_LOG"
  chmod 600 "$SETUP_LOG"
  exec > >(tee -a "$SETUP_LOG") 2>&1
fi
progress_start

step platform "Platform and execution mode"
MODEL=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
UNAME_M=$(uname -m)
printf 'Device: %s (%s)\nMode: %s\n' "$MODEL" "$UNAME_M" "$([[ $CHECK_ONLY == 1 ]] && echo check-only || echo setup)"
[[ "$MODEL" == *"Raspberry Pi 5"* ]] || warn "This script is tuned for Raspberry Pi 5."
[[ "$UNAME_M" == "aarch64" ]] || fail "aarch64 is required; found $UNAME_M."
stop_if_failed

step system "System build and runtime libraries"
PACKAGES=(
  build-essential cmake ninja-build pkg-config git curl ca-certificates rsync
  python3 python3-dev python3-pip python3-venv ffmpeg v4l-utils alsa-utils
  libsdl2-dev libegl-dev libgles-dev libportaudio2 portaudio19-dev libudev-dev libssl-dev
)
MISSING=()
if ((SKIP_SYSTEM == 0)); then
  for package in "${PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || MISSING+=("$package")
  done
  if ((${#MISSING[@]})); then
    printf 'Missing packages: %s\n' "${MISSING[*]}"
    if ((CHECK_ONLY)); then
      fail "System packages are missing."
    elif confirm "Install the missing system packages with apt?"; then
      need_sudo
      root_run apt-get update
      root_run apt-get install -y "${MISSING[@]}"
      ok "System packages installed."
    else
      fail "System packages were declined."
    fi
  else
    ok "All required system packages are installed."
  fi
else
  warn "System package installation skipped."
fi
stop_if_failed

step venv "uv and Python virtual environment"
UV=${UV:-$HOME/.local/bin/uv}
if [[ ! -x "$UV" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV=$(command -v uv)
  elif ((CHECK_ONLY)); then
    fail "uv is not installed at $UV."
  elif confirm "Install uv under $HOME/.local/bin?"; then
    curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
  else
    fail "uv installation was declined."
  fi
fi
if [[ -x "$UV" ]] || command -v "$UV" >/dev/null 2>&1; then
  if ((CHECK_ONLY)); then
    [[ -x .venv/bin/python ]] || fail "The .venv is missing."
  else
    "$UV" venv --system-site-packages --python /usr/bin/python3 --allow-existing .venv
    "$UV" pip install --python .venv/bin/python -e '.[dev]'
  fi
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -c "import hailo_platform, fastapi; print('Python imports OK; FastAPI', fastapi.__version__)" || fail "Python imports failed."
  fi
fi
stop_if_failed

step hailo "HailoRT $EXPECTED_HAILO stack and Hailo-10H"
HAILO_HINT="Install the HailoRT $EXPECTED_HAILO package set (hailort, hailort-pcie-driver and the \
hailort Python wheel) from https://hailo.ai/developer-zone/ and reboot. The Raspberry Pi apt \
repository only ships 5.1.x, so do not use hailo-h10-all for this stack."
hailo_version() {
  local name=$1 found=$2
  if [[ "$found" == "$EXPECTED_HAILO" ]]; then
    ok "$name $found"
  else
    fail "$name ${found:-not found} does not match $EXPECTED_HAILO."
  fi
}
if ! command -v hailortcli >/dev/null 2>&1; then
  fail "hailortcli is missing."
else
  hailo_version "HailoRT CLI" "$(hailortcli --version 2>&1 | sed -n 's/.*version \([^ ]*\).*/\1/p' | head -n1)"
  FW_INFO=$(hailortcli fw-control identify 2>&1 || true)
  hailo_version "Hailo-10H firmware" "$(sed -n 's/.*Firmware Version: \([^ ]*\).*/\1/p' <<<"$FW_INFO" | head -n1)"
  grep -q 'HAILO10H' <<<"$FW_INFO" || fail "The detected Hailo device is not HAILO10H."
fi
hailo_version "HailoRT PCIe driver (hailo1x_pci)" "$(cat /sys/module/hailo1x_pci/version 2>/dev/null || true)"
hailo_version "HailoRT Python binding" \
  "$(/usr/bin/python3 -c 'from importlib.metadata import version; print(version("hailort"))' 2>/dev/null || true)"
((FAILURES == 0)) || printf '%s\n' "$HAILO_HINT" >&2
stop_if_failed

step ollama "hailo-ollama $EXPECTED_HAILO"
# Hailo publishes the hailo-gen-ai-model-zoo .deb only behind a Developer Zone login, so a missing
# or mismatched hailo-ollama is built from the matching public source tag instead.
OLLAMA_MARK=/usr/local/share/hailo-ollama/VERSION
OLLAMA_SRC=$ROOT/runtime/build/hailo_model_zoo_genai
OLLAMA_DEB=$( (dpkg-query -W -f='${Status} ${Version}\n' hailo-gen-ai-model-zoo 2>/dev/null || true) \
  | awk '$3 == "installed" {print $4}')
if [[ -n "$OLLAMA_DEB" ]]; then
  OLLAMA_VERSION=$OLLAMA_DEB
else
  OLLAMA_VERSION=$(cat "$OLLAMA_MARK" 2>/dev/null || true)
fi
install_hailo_ollama() {
  local tag=v$EXPECTED_HAILO qwen3
  need_sudo
  if [[ -n "$OLLAMA_DEB" ]]; then
    # Its manifests under /usr/share would mix with the source build's under /usr/local/share.
    root_run apt-get remove -y --allow-change-held-packages hailo-gen-ai-model-zoo
  fi
  rm -rf "$OLLAMA_SRC"
  mkdir -p "$(dirname "$OLLAMA_SRC")"
  git clone -q --depth 1 --branch "$tag" https://github.com/hailo-ai/hailo_model_zoo_genai.git "$OLLAMA_SRC"
  # The v5.3.0 source keeps qwen3's manifest one level too high, so hailo-ollama would list it as
  # "manifests:qwen3". The packaged release installs it as qwen3/1.7b.
  qwen3=$OLLAMA_SRC/models/manifests/qwen3
  if [[ -f "$qwen3/manifest.json" && ! -e "$qwen3/1.7b" ]]; then
    mkdir -p "$qwen3/1.7b"
    mv "$qwen3/manifest.json" "$qwen3/1.7b/manifest.json"
  fi
  cmake -S "$OLLAMA_SRC" -B "$OLLAMA_SRC/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$OLLAMA_SRC/build" -j3
  root_run cmake --install "$OLLAMA_SRC/build" --prefix /usr/local
  printf '%s\n' "$EXPECTED_HAILO" | root_run tee "$OLLAMA_MARK" >/dev/null
  hash -r
  ok "Installed hailo-ollama $EXPECTED_HAILO: $(command -v hailo-ollama)"
}
if command -v hailo-ollama >/dev/null 2>&1 && [[ "$OLLAMA_VERSION" == "$EXPECTED_HAILO" ]]; then
  ok "hailo-ollama $OLLAMA_VERSION: $(command -v hailo-ollama)"
elif ((CHECK_ONLY)); then
  fail "hailo-ollama ${OLLAMA_VERSION:-is not installed}; $EXPECTED_HAILO is required."
elif [[ -n "$OLLAMA_DEB" ]] && ! confirm "Replace the hailo-gen-ai-model-zoo $OLLAMA_DEB package with a hailo-ollama $EXPECTED_HAILO source build?"; then
  fail "hailo-ollama $OLLAMA_DEB was kept; $EXPECTED_HAILO is required."
elif [[ -z "$OLLAMA_DEB" ]] && ! confirm "Build and install hailo-ollama $EXPECTED_HAILO from Hailo's source (about 4 minutes)?"; then
  fail "hailo-ollama installation was declined."
else
  install_hailo_ollama
fi
stop_if_failed

download() {
  local url=$1 dest=$2
  [[ -s "$dest" ]] && { ok "Already present: $dest"; return 0; }
  if ((CHECK_ONLY)); then fail "Missing model: $dest"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  printf 'Downloading (resumable): %s\n' "$url"
  curl -fL --retry 3 --continue-at - --output "$dest.part" "$url"
  mv "$dest.part" "$dest"
}

step hefs "Hailo-10H model files (HEFs)"
HEF_DIR=${AIBOX_HEF_DIR:-/usr/local/hailo/resources/models/hailo10h}
HEF_MZ=https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v$EXPECTED_HAILO/hailo10h
HEF_S3=https://hailo-csdata.s3.eu-west-2.amazonaws.com/resources/hefs/h10
HEF_GENAI=https://dev-public.hailo.ai/v$EXPECTED_HAILO/blob
# Keep Whisper on the same release as the vision HEFs. The v5.1.1 build transcribes the XVF3800
# cabin corpus slightly better (47% vs 55% WER), but next to the v5.3.0 vision set it leaves no
# room on the NPU for Qwen3-VL (HAILO_RESOURCE_EXHAUSTED).
WHISPER_ZOO=${AIBOX_WHISPER_ZOO:-v5.3.0}
case "$WHISPER_ZOO" in
  v5.1.1) WHISPER_BYTES=136889274 ;;
  v5.3.0) WHISPER_BYTES=130206034 ;;
  *) WHISPER_BYTES="" ;;
esac
# url|bytes|build. The byte size identifies the build offline: it differs in every Model Zoo
# release (5.1, 5.2 and 5.3 builds of the same model were all different sizes).
declare -A HEFS=(
  [yolov8m.hef]="$HEF_MZ/yolov8m.hef|26738688|Model Zoo v5.3.0"
  [yolov8m_pose.hef]="$HEF_MZ/yolov8m_pose.hef|29335552|Model Zoo v5.3.0"
  [yolov5m_seg.hef]="$HEF_MZ/yolov5m_seg.hef|18522112|Model Zoo v5.3.0"
  [scrfd_10g.hef]="$HEF_MZ/scrfd_10g.hef|4620288|Model Zoo v5.3.0"
  [ufld_v2_tu.hef]="$HEF_S3/ufld_v2_tu.hef|64525344|Hailo-10H resources"
  [ocr_det.hef]="$HEF_S3/ocr_det.hef|7372679|Hailo-10H resources"
  [ocr.hef]="$HEF_S3/ocr.hef|7330636|Hailo-10H resources"
  [Whisper-Base.hef]="https://dev-public.hailo.ai/$WHISPER_ZOO/blob/Whisper-Base.hef|$WHISPER_BYTES|GenAI $WHISPER_ZOO"
  [Qwen2-VL-2B-Instruct.hef]="$HEF_GENAI/Qwen2-VL-2B-Instruct.hef|3255444179|GenAI v5.3.0"
  [Qwen3-VL-2B-Instruct.hef]="$HEF_GENAI/Qwen3-VL-2B-Instruct.hef|3185773502|GenAI v5.3.0"
)
HEF_EXPECTED=0
HEF_OTHER=0
place_file() {
  # Installs src at dest; a file it replaces is kept in <dir>/.aibox-replaced/.
  local src=$1 dest=$2 dir
  local -a run=()
  dir=$(dirname "$dest")
  mkdir -p "$dir" 2>/dev/null || true
  if [[ ! -w "$dir" ]]; then
    need_sudo
    run=(root_run)
  fi
  "${run[@]}" mkdir -p "$dir"
  if [[ -e "$dest" ]]; then
    "${run[@]}" mkdir -p "$dir/.aibox-replaced"
    "${run[@]}" mv "$dest" "$dir/.aibox-replaced/"
    printf '  previous file kept in %s/.aibox-replaced/\n' "$dir"
  fi
  "${run[@]}" install -m 644 "$src" "$dest"
  rm -f "$src"
}
fetch_hef() {
  local name=$1 dest=${2:-$HEF_DIR/$1} url bytes build size tmp=$ROOT/runtime/downloads/$1
  IFS='|' read -r url bytes build <<<"${HEFS[$name]}"
  # The pinned sizes describe the 5.3.0 builds only.
  [[ "$EXPECTED_HAILO" == "5.3.0" ]] || bytes=""
  if [[ -s "$dest" ]]; then
    size=$(stat -c %s "$dest")
    if [[ -z "$bytes" || "$size" == "$bytes" ]]; then
      ok "$name: ${build} build"
      HEF_EXPECTED=$((HEF_EXPECTED + 1))
      return 0
    fi
    if ((CHECK_ONLY)); then
      fail "$name is a different build ($size bytes); setup installs the $build build."
      return 0
    fi
    if ! confirm "$name is a different build ($size bytes). Replace it with the $build build?"; then
      warn "$name: kept the existing build."
      HEF_OTHER=$((HEF_OTHER + 1))
      return 0
    fi
  elif ((CHECK_ONLY)); then
    fail "Missing: $dest"
    return 0
  fi
  download "$url" "$tmp"
  size=$(stat -c %s "$tmp")
  if [[ -n "$bytes" && "$size" != "$bytes" ]]; then
    rm -f "$tmp"
    fail "$name downloaded $size bytes, not the expected $bytes; the source may have changed."
    return 0
  fi
  place_file "$tmp" "$dest"
  ok "$name: installed the $build build"
  HEF_EXPECTED=$((HEF_EXPECTED + 1))
}
if [[ ! -d "$HEF_DIR" ]] && ((CHECK_ONLY == 0)); then
  need_sudo
  root_run mkdir -p "$HEF_DIR"
fi
[[ -d "$HEF_DIR" ]] || fail "HEF directory not found: $HEF_DIR"
for hef in yolov8m.hef yolov8m_pose.hef yolov5m_seg.hef ufld_v2_tu.hef scrfd_10g.hef ocr_det.hef ocr.hef Whisper-Base.hef; do
  fetch_hef "$hef"
done
stop_if_failed

step models "Models with resumable downloads"
QWEN3_HEF=${AIBOX_QWEN3_VL_HEF:-$HOME/Downloads/Qwen3-VL-2B-Instruct.hef}
PIPER_VOICE=${AIBOX_PIPER_VOICE:-$ROOT/models/piper/en_US-amy-low.onnx}
OLLAMA_PULL_PORT=${AIBOX_OLLAMA_PULL_PORT:-8001}
ollama_pull() {
  # hailo-ollama has no pull subcommand (extra arguments just start the server), so pull over its
  # HTTP API from a temporary instance on a spare port.
  if [[ -z "$OLLAMA_PULL_PID" ]]; then
    OLLAMA_HOST=127.0.0.1:$OLLAMA_PULL_PORT hailo-ollama >"$ROOT/runtime/logs/ollama-pull.log" 2>&1 &
    OLLAMA_PULL_PID=$!
    for _ in $(seq 1 50); do
      curl -fs -m 1 -o /dev/null "http://127.0.0.1:$OLLAMA_PULL_PORT/api/tags" && break
      sleep 0.2
    done
  fi
  curl -fsS --no-buffer "http://127.0.0.1:$OLLAMA_PULL_PORT/api/pull" -H 'Content-Type: application/json' \
    -d "{\"model\": \"$1\", \"stream\": true}" | grep -o '"status":"[^"]*"' | uniq || true
}
if ((SKIP_MODELS == 0)); then
  fetch_hef Qwen3-VL-2B-Instruct.hef "$QWEN3_HEF"
  fetch_hef Qwen2-VL-2B-Instruct.hef
  download "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx" "$PIPER_VOICE"
  download "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json" "$PIPER_VOICE.json"
  data_home=${XDG_DATA_HOME:-$HOME/.local/share}
  for model in qwen3:1.7b qwen2.5:1.5b llama3.2:1b qwen2:1.5b; do
    family=${model%%:*}; tag=${model##*:}
    manifest=""
    for share in /usr/share /usr/local/share; do
      candidate="$share/hailo-ollama/models/manifests/$family/$tag/manifest.json"
      [[ -s "$candidate" ]] && { manifest=$candidate; break; }
    done
    if [[ -z "$manifest" ]]; then
      fail "Missing hailo-ollama manifest for $model."
      continue
    fi
    digest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("hef_h10h", ""))' "$manifest")
    blob="$data_home/hailo-ollama/models/blob/sha256_$digest"
    if [[ -s "$blob" ]]; then
      ok "Ollama model present: $model"
    elif ((CHECK_ONLY)); then
      fail "Ollama model blob missing: $model"
    elif confirm "Download $model through hailo-ollama?"; then
      ollama_pull "$model"
      [[ -s "$blob" ]] && ok "Ollama model pulled: $model" || fail "Ollama pull did not produce $blob."
    else
      fail "Model download declined: $model"
    fi
  done
  if [[ -n "$OLLAMA_PULL_PID" ]]; then
    kill "$OLLAMA_PULL_PID" 2>/dev/null || true
    wait "$OLLAMA_PULL_PID" 2>/dev/null || true
    OLLAMA_PULL_PID=""
  fi
else
  warn "Model downloads/checks skipped."
fi
stop_if_failed

step videos "Preset road videos"
shopt -s nullglob
PRESET_SOURCES=("$HOME"/Downloads/gettyimages*.mp4)
shopt -u nullglob
if ((${#PRESET_SOURCES[@]} == 0)); then
  warn "No gettyimages*.mp4 presets found in ~/Downloads; upload videos through Computer Vision instead."
else
  for i in "${!PRESET_SOURCES[@]}"; do
    n=$((i + 1)); src=${PRESET_SOURCES[$i]}; dest="$ROOT/data/videos/road_${n}.mp4"
    if [[ -e "$dest" ]]; then
      ok "Preset road_${n} is present."
    elif ((CHECK_ONLY)); then
      fail "Preset road_${n} is not linked in data/videos."
    else
      mkdir -p "$ROOT/data/videos"
      ln -s "$src" "$dest"
      ok "Linked road_${n} to $(basename "$src")."
    fi
  done
fi
stop_if_failed

RSID_LIB=${AIBOX_REALSENSE_LIB:-$RSID_REPO/build-device/lib}
if ((SKIP_REALSENSE == 0)); then
  step realsense "Intel RealSense ID SDK $EXPECTED_RSID_SDK"
  if [[ ! -d "$RSID_REPO" ]]; then
    if ((CHECK_ONLY)); then
      fail "RealSense ID repository missing: $RSID_REPO"
    elif confirm "Clone RealSense ID $RSID_GIT_REF into $RSID_REPO?"; then
      mkdir -p "$(dirname "$RSID_REPO")"
      git clone --depth 1 --branch "$RSID_GIT_REF" https://github.com/realsenseai/RealSenseID.git "$RSID_REPO"
    else
      fail "RealSense ID clone was declined."
    fi
  fi
  if [[ -d "$RSID_REPO" ]]; then
    if ((CHECK_ONLY)); then
      compgen -G "$RSID_LIB/rsid_py*.so" >/dev/null || fail "RealSense Python SDK is not built in $RSID_LIB."
    else
      cmake -S "$RSID_REPO" -B "$RSID_REPO/build-device" -DCMAKE_BUILD_TYPE=Release \
        -DRSID_PY=ON -DRSID_SAMPLES=ON -DRSID_PIPELINE=OFF
      cmake --build "$RSID_REPO/build-device" -j2
    fi
    if [[ -d "$RSID_LIB" ]]; then
      RSID_VERSION=$(PYTHONPATH="$RSID_LIB" LD_LIBRARY_PATH="$RSID_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        python3 -c 'import rsid_py; print(rsid_py.__version__)' 2>/dev/null || true)
      [[ "$RSID_VERSION" == "$EXPECTED_RSID_SDK" ]] || fail "RealSense SDK $RSID_VERSION does not match $EXPECTED_RSID_SDK."
    fi
  fi
  stop_if_failed

  step peripherals "F455 firmware, USB power and peripherals"
  RSID_CLI="$RSID_REPO/build-device/bin/rsid-cli"
  [[ -x "$RSID_CLI" ]] || fail "RealSense CLI not found: $RSID_CLI"
  if [[ -x "$RSID_CLI" ]]; then
    RSID_INFO=$(printf 'v\nq\n' | timeout 15 "$RSID_CLI" 2>&1 || true)
    OPFW=$(sed -n 's/^OPFW[[:space:]]*:[[:space:]]*\([^[:space:]]*\).*/\1/p' <<<"$RSID_INFO" | head -n1)
    printf 'F455 OPFW: %s (required %s)\n' "${OPFW:-not detected}" "$EXPECTED_RSID_FW"
    if [[ "$OPFW" != "$EXPECTED_RSID_FW" ]]; then
      if [[ -n "$FIRMWARE_IMAGE" ]] && ((CHECK_ONLY == 0)); then
        [[ -s "$FIRMWARE_IMAGE" ]] || { echo "Firmware image not found: $FIRMWARE_IMAGE" >&2; exit 1; }
        warn "Old F455 firmware can require staged, SKU-compatible signed updates. Do not force an incompatible image."
        if confirm "Run the vendor F455 updater with $FIRMWARE_IMAGE?"; then
          "$RSID_REPO/build-device/bin/rsid-fw-update" --file "$FIRMWARE_IMAGE" --interactive
        else
          fail "F455 firmware update was declined."
        fi
      else
        fail "F455 firmware mismatch. Supply a compatible signed image with --firmware-image; older units may need staged updates."
      fi
    else
      ok "F455 firmware matches $EXPECTED_RSID_FW."
    fi
  fi
  if ! grep -q '^usb_max_current_enable=1[[:space:]]*$' /boot/firmware/config.txt 2>/dev/null; then
    if ((CHECK_ONLY)); then
      fail "usb_max_current_enable=1 is missing from /boot/firmware/config.txt."
    elif confirm "Enable the Pi's full USB current budget (requires reboot)?"; then
      need_sudo
      root_run cp /boot/firmware/config.txt "/boot/firmware/config.txt.aibox-backup"
      printf '\nusb_max_current_enable=1\n' | root_run tee -a /boot/firmware/config.txt >/dev/null
      REBOOT_REQUIRED=1
      ok "USB current setting added."
    else
      fail "USB current setup was declined."
    fi
  else
    ok "USB full-current setting is present."
  fi
fi

find_camera() {
  local pattern=$1 path
  for path in /dev/v4l/by-id/$pattern; do
    [[ -e "$path" ]] && { printf '%s\n' "$path"; return 0; }
  done
  return 1
}
C920E=$(find_camera 'usb-046d_Logi_Webcam_C920e_*-video-index0' || true)
F455=$(find_camera 'usb-Intel_*-video-index0' || true)
[[ -n "$C920E" ]] && ok "C920e: $C920E" || fail "C920e UVC device not found."
[[ -n "$F455" ]] && ok "F455 RGB: $F455" || fail "F455 UVC device not found."
[[ -e /dev/ttyACM0 ]] && ok "F455 serial: /dev/ttyACM0" || fail "F455 serial device /dev/ttyACM0 not found."
if command -v arecord >/dev/null 2>&1 && arecord -l | grep -qi 'xvf3800'; then
  ok "XVF3800 capture device found."
else
  fail "XVF3800 microphone not found in arecord -l."
fi
if command -v aplay >/dev/null 2>&1 && aplay -l | grep -qi 'xvf3800'; then
  ok "XVF3800 playback device found."
else
  warn "XVF3800 playback device not found; check voice output."
fi
if [[ -S /var/run/gpsd.sock ]]; then
  ok "gpsd socket found (satellite fix still depends on location)."
else
  warn "gpsd is not available; GPS features will be unavailable."
fi
stop_if_failed

step lvgl "LVGL Studio build"
if ((CHECK_ONLY)); then
  [[ -x studio_lvgl/build-gpu/aibox_studio ]] || fail "LVGL GPU Studio binary is not built."
else
  cmake -S studio_lvgl -B studio_lvgl/build-gpu -G Ninja -DSTUDIO_RENDER=gpu -DCMAKE_BUILD_TYPE=Release
  cmake --build studio_lvgl/build-gpu -j2
fi
[[ -x studio_lvgl/build-gpu/aibox_studio ]] && ok "LVGL Studio is built."
stop_if_failed

if ((SKIP_TESTS == 0)); then
  step tests "Non-hardware regression tests"
  if ((CHECK_ONLY == 0)); then
    .venv/bin/pytest -q -m 'not pi'
  else
    ok "Tests are run during setup, not in --check mode."
  fi

  step device "On-device model check (every model on the Hailo-10H)"
  if ((CHECK_ONLY)); then
    ok "Runs during setup, not in --check mode."
  elif curl -fs -m 1 -o /dev/null "http://127.0.0.1:${AIBOX_HTTP_PORT:-8442}/health"; then
    warn "The AI Box server is running and owns the NPU. Stop it and rerun setup to check the models."
  elif confirm "Load and run every model on the Hailo-10H now (about 4 minutes)?"; then
    # The hardware tests talk to hailo-ollama on :8000; it must share the device with them.
    if ! curl -fs -m 1 -o /dev/null http://127.0.0.1:8000/api/tags; then
      HAILO_OLLAMA_VDEVICE_GROUP_ID=SHARED hailo-ollama >"$ROOT/runtime/logs/ollama-device-check.log" 2>&1 &
      OLLAMA_TEST_PID=$!
      for _ in $(seq 1 50); do
        curl -fs -m 1 -o /dev/null http://127.0.0.1:8000/api/tags && break
        sleep 0.2
      done
    fi
    if .venv/bin/pytest -q -m pi; then
      ok "Every model loaded and ran on the Hailo-10H."
    else
      fail "A model failed on the Hailo-10H (output above). Another hailo-ollama on port 8000 must run with HAILO_OLLAMA_VDEVICE_GROUP_ID=SHARED."
    fi
    if [[ -n "$OLLAMA_TEST_PID" ]]; then
      kill "$OLLAMA_TEST_PID" 2>/dev/null || true
      wait "$OLLAMA_TEST_PID" 2>/dev/null || true
      OLLAMA_TEST_PID=""
    fi
  else
    warn "On-device model check skipped."
  fi
  stop_if_failed
fi

step apikey "LAN API authentication"
if [[ -f .env ]]; then
  ok "Existing .env preserved; the launcher will use it without printing secrets."
elif ((CHECK_ONLY)); then
  fail "No .env found; LAN API authentication is not configured."
elif confirm "Generate a random LAN API key in .env (owner-only permissions)?"; then
  umask 077
  API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  printf 'AIBOX_API_KEY=%s\n' "$API_KEY" > .env
  chmod 600 .env
  ok "Generated $ROOT/.env with a random API key."
  printf '\nWeb Studio API key (shown once):\n%s\n\n' "$API_KEY"
  unset API_KEY
else
  warn "No API key configured; LAN inference APIs will be open."
fi
cat <<'EOF'

  About the API key
  The AI Box serves its APIs to the whole LAN (https://<pi-ip>:8443). The key keeps other
  devices on the same network from using the NPU, cameras, and microphone.
  - Web Studio on a laptop or phone: paste the key when it asks (stored only in that browser).
  - Scripts and curl: send  Authorization: Bearer <key>
  - Apps on the Pi itself (LVGL Studio, http://127.0.0.1:8442) need no key.
  It is stored in .env with owner-only permissions. Show it: grep AIBOX_API_KEY .env
  New key: delete .env and run setup again, then re-enter it in each browser.
EOF

if ((CHECK_ONLY == 0)); then
  step setupenv "Write runtime setup.env (no secrets)"
  {
    printf 'AIBOX_HEF_DIR=%q\n' "$HEF_DIR"
    printf 'AIBOX_QWEN3_VL_HEF=%q\n' "$QWEN3_HEF"
    printf 'AIBOX_PIPER_VOICE=%q\n' "$PIPER_VOICE"
    printf 'AIBOX_REALSENSE_LIB=%q\n' "$RSID_LIB"
    printf 'AIBOX_CAMERA_C920E=%q\n' "$C920E"
    printf 'AIBOX_CAMERA_F455=%q\n' "$F455"
  } > runtime/setup.env
  ok "Wrote $ROOT/runtime/setup.env"
fi

step finish "Setup complete"
ok "HailoRT $EXPECTED_HAILO (CLI, driver, firmware, Python) and hailo-ollama $EXPECTED_HAILO."
if ((HEF_OTHER)); then
  warn "Model files: $HEF_EXPECTED expected builds, $HEF_OTHER kept at a different build."
else
  ok "Model files: all $HEF_EXPECTED are the expected builds (Whisper-Base: GenAI $WHISPER_ZOO)."
fi
((SKIP_REALSENSE)) || ok "RealSense SDK $EXPECTED_RSID_SDK, F455 OPFW $EXPECTED_RSID_FW."
printf 'Start with: cd %s && scripts/run_aibox.sh\n' "$ROOT"
printf 'Live benchmark: %s/scripts/bench_live.py\n' "$ROOT"
