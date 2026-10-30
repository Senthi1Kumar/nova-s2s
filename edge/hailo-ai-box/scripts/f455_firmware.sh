#!/usr/bin/env bash
# Staged RealSense ID F450/F455 firmware update to the OPFW version Face ID needs. Run ON the Pi,
# after scripts/pi_setup.sh has built RealSense ID SDK 3.6.1.
#
# Each firmware generation is flashed with the SDK release of its era. The old updaters are built
# from their release tags and the signed images come from Intel's GitHub releases at run time;
# neither is stored in this repository.
#
#   scripts/f455_firmware.sh                  show the plan, then flash hop by hop (asks each time)
#   scripts/f455_firmware.sh --plan           show the plan only
#   scripts/f455_firmware.sh --dry-run        build the updaters and download the images, no flashing
#   scripts/f455_firmware.sh --from 1.6.1.1007 --dry-run   rehearse the plan for another version
set -Eeuo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RSID_REPO=${AIBOX_REALSENSE_REPO:-$HOME/repos/RealSenseID}
PORT=${AIBOX_F455_PORT:-/dev/ttyACM0}
TARGET=9.16.1.1
RSID_GIT=https://github.com/realsenseai/RealSenseID.git
RELEASES=https://github.com/realsenseai/RealSenseID/releases/download
WORK=$ROOT/runtime/build/realsense-fw
DOWNLOADS=$ROOT/runtime/downloads/f455
PLAN_ONLY=0
DRY_RUN=0
FROM=""

while (($#)); do
  case "$1" in
    --plan) PLAN_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --from) FROM=${2:?--from needs a version}; DRY_RUN=1; shift ;;
    --port) PORT=${2:?--port needs a device}; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1;34m━━ %s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
confirm() {
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}
version_lt() { [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]; }

RSID_CLI=$RSID_REPO/build-device/bin/rsid-cli
# The 9.16.1.1 image also carries the face networks; Face ID needs all of them, not just OPFW.
declare -A TARGET_MODULES=([OPFW]=$TARGET [NNLED]=19.8.25.2 [DNET]=26.2.25.0 [RECOG]=29.12.25.0)
declare -A MODULES=()
read_modules() {
  local name version
  MODULES=()
  while read -r name version; do
    MODULES[$name]=$version
  done < <(printf 'v\nq\n' | timeout 15 "$RSID_CLI" 2>&1 \
    | sed -n 's/^\([A-Z0-9]*\)[[:space:]]*:[[:space:]]*\([0-9][0-9.]*\).*/\1 \2/p' || true)
}
modules_behind() {
  local name
  for name in OPFW NNLED DNET RECOG; do
    [[ "${MODULES[$name]:-}" == "${TARGET_MODULES[$name]}" ]] \
      || printf '%s %s->%s  ' "$name" "${MODULES[$name]:-?}" "${TARGET_MODULES[$name]}"
  done
}
wait_for_camera() {
  printf 'Waiting for the camera to restart...\n'
  for _ in $(seq 1 30); do
    read_modules
    [[ -n "${MODULES[OPFW]:-}" ]] && return 0
    sleep 2
  done
  die "The camera did not come back after flashing; do not unplug it. Power-cycle the Pi, then rerun this script."
}

# Mirrors the serial-number rule in SDK 3.6.1 (src/FwUpdate/F45x/FwUpdaterF45x.cc).
sku_for_serial() {
  local sn=$1
  if [[ "$sn" =~ ^12[02][0-9]6228[0-9]{4} || "$sn" =~ ^[0-9]{4}6229[0-9]{4} \
        || "$sn" =~ ^[0-9]{4}62[3-9][0-9]{5} ]]; then
    echo 2
  else
    echo 1
  fi
}

build_updater() {
  local tag=$1 tool=$2 src=$WORK/$1
  if [[ ! -x "$src/build/bin/$tool" ]]; then
    printf 'Building %s from RealSense ID %s (about 2 minutes)...\n' "$tool" "$tag"
    [[ -d "$src/.git" ]] || git clone -q --depth 1 --branch "$tag" "$RSID_GIT" "$src"
    # The 2021 sources predate GCC 13+, which no longer pulls these headers in transitively.
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DRSID_TOOLS=ON -DRSID_EXAMPLES=OFF \
      -DRSID_SAMPLES=OFF -DRSID_PREVIEW=OFF -DRSID_SECURE=OFF \
      -DCMAKE_CXX_FLAGS="-include cstdint -include cstring -include cstdlib" >"$src.cmake.log" 2>&1 \
      || { tail -n 20 "$src.cmake.log"; die "Configuring RealSense ID $tag failed (log: $src.cmake.log)."; }
    cmake --build "$src/build" --target "$tool" -j3 >"$src.build.log" 2>&1 \
      || { tail -n 20 "$src.build.log"; die "Building $tool from $tag failed (log: $src.build.log)."; }
  fi
  ok "$tool ($tag) ready."
}

fetch_image() {
  local tag=$1 name=$2 dest=$DOWNLOADS/$2
  if [[ ! -s "$dest" ]]; then
    mkdir -p "$DOWNLOADS"
    printf 'Downloading %s from the %s release...\n' "$name" "$tag"
    curl -fL --retry 3 --continue-at - --output "$dest.part" "$RELEASES/$tag/$name" \
      || die "Download failed: $RELEASES/$tag/$name"
    mv "$dest.part" "$dest"
  fi
  ok "$name ($(du -h "$dest" | cut -f1))"
}

step "F450/F455 camera"
[[ -x "$RSID_CLI" ]] || die "RealSense ID SDK 3.6.1 is not built at $RSID_REPO; run scripts/pi_setup.sh first."
shopt -s nullglob
CAMS=(/dev/v4l/by-id/usb-Intel_Intel_F45*-video-index0)
shopt -u nullglob
((${#CAMS[@]})) || die "No F450/F455 camera found in /dev/v4l/by-id; check its USB cable."
SERIAL=${CAMS[0]##*/usb-Intel_Intel_F45?_}
SERIAL=${SERIAL%-video-index0}
SKU=$(sku_for_serial "$SERIAL")
if [[ -n "$FROM" ]]; then
  CURRENT=$FROM
  warn "Rehearsal: planning as if the camera reported OPFW $FROM (nothing is flashed)."
else
  read_modules
  CURRENT=${MODULES[OPFW]:-}
  [[ -n "$CURRENT" ]] || die "The camera did not report its firmware; check $PORT and the USB cable."
fi
ok "Serial $SERIAL -> SKU $SKU, OPFW $CURRENT (Face ID needs $TARGET)."

# tag|updater|image|OPFW after the hop
HOPS=()
version_lt "$CURRENT" 2.0.10.45 && HOPS+=("v0.12.4|fw-updater-cli|F450_2.0.10.45_signed.bin|2.0.10.45")
version_lt "$CURRENT" 3.1.0.29 && HOPS+=("v0.19.0|rsid-fw-update|F450_3.1.0.29_SKU${SKU}_SIGNED.bin|3.1.0.29")
if version_lt "$TARGET" "$CURRENT"; then
  die "OPFW $CURRENT is newer than $TARGET; this script does not downgrade."
elif [[ "$CURRENT" != "$TARGET" ]]; then
  HOPS+=("v3.6.1|rsid-fw-update|F450_${TARGET}_SKU${SKU}_SIGNED.bin|$TARGET")
elif [[ -z "$FROM" && -n "$(modules_behind)" ]]; then
  # An interrupted 9.16 update leaves OPFW current and the face networks old.
  warn "OPFW is $TARGET but the face networks are not: $(modules_behind)"
  HOPS+=("v3.6.1|rsid-fw-update|F450_${TARGET}_SKU${SKU}_SIGNED.bin|$TARGET")
fi
if ((${#HOPS[@]} == 0)); then
  ok "Firmware is already $TARGET, including the face networks; nothing to do."
  exit 0
fi

step "Plan: ${#HOPS[@]} hop(s)"
for i in "${!HOPS[@]}"; do
  IFS='|' read -r tag tool image after <<<"${HOPS[$i]}"
  printf '  %d. %-14s %-34s -> OPFW %s\n' "$((i + 1))" "$tag $tool" "$image" "$after"
done
cat <<'EOF'
  On a camera older than 9.16, the last hop runs twice: the old firmware rejects the larger
  9.16 face networks (err 7: NNLED size), and the second pass installs them under 9.16.
  Each hop takes 1-2 minutes. Do not unplug the camera or power off the Pi while it flashes.
  An update can erase faces enrolled on the camera; re-enroll them in Face ID afterwards.
EOF
((PLAN_ONLY)) && exit 0
if [[ -z "$FROM" ]] && curl -fs -m 1 -o /dev/null "http://127.0.0.1:${AIBOX_HTTP_PORT:-8442}/health"; then
  die "The AI Box server is running and may hold the camera; stop scripts/run_aibox.sh first."
fi

step "Updaters and signed images"
for hop in "${HOPS[@]}"; do
  IFS='|' read -r tag tool image after <<<"$hop"
  [[ "$tag" == v3.6.1 ]] || build_updater "$tag" "$tool"
  fetch_image "$tag" "$image"
done
if ((DRY_RUN)); then
  ok "Dry run complete: updaters built and images downloaded; nothing was flashed."
  exit 0
fi

flash() {
  local tag=$1 tool=$2 image=$3 bin lib
  if [[ "$tag" == v3.6.1 ]]; then
    bin=$RSID_REPO/build-device/bin/$tool lib=$RSID_REPO/build-device/lib
  else
    bin=$WORK/$tag/build/bin/$tool lib=$WORK/$tag/build/lib
  fi
  LD_LIBRARY_PATH="$lib" "$bin" --file "$DOWNLOADS/$image" --port "$PORT" --interactive
}

read_modules
for i in "${!HOPS[@]}"; do
  IFS='|' read -r tag tool image after <<<"${HOPS[$i]}"
  step "Hop $((i + 1))/${#HOPS[@]}: OPFW ${MODULES[OPFW]:-?} -> $after"
  confirm "Flash $image with the $tag updater now?" || die "Stopped before hop $((i + 1)); the camera is unchanged."
  if [[ "$tag" != v3.6.1 ]]; then
    flash "$tag" "$tool" "$image" \
      || die "The $tag updater reported an error; do not unplug the camera. Rerun this script to resume."
    wait_for_camera
    [[ "${MODULES[OPFW]}" == "$after" ]] \
      || die "After hop $((i + 1)) the camera reports OPFW ${MODULES[OPFW]}, expected $after. Rerun this script."
    ok "OPFW is now $after."
    continue
  fi
  for pass in 1 2; do
    flash "$tag" "$tool" "$image" || warn "Pass $pass of the $tag updater ended with an error (see above)."
    wait_for_camera
    [[ -z "$(modules_behind)" ]] && break
    if ((pass == 1)) && [[ "${MODULES[OPFW]}" == "$TARGET" ]]; then
      warn "OPFW is now $TARGET; the face networks still need the second pass: $(modules_behind)"
      confirm "Run the second pass now?" || die "Stopped. Rerun this script to finish the face networks."
    else
      die "The camera is not fully on $TARGET: $(modules_behind). Rerun this script; if it repeats, keep the log above."
    fi
  done
  ok "OPFW and face networks are now $TARGET."
done
ok "F455 firmware is $TARGET. Face ID is ready; re-enroll faces if the update erased them."
