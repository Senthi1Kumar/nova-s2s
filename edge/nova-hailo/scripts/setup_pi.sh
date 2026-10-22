#!/usr/bin/env bash
# One-time setup of the CabinEnv demo on a Raspberry Pi 5 with the AI HAT+ 2 (Hailo-10H),
# Raspberry Pi OS Trixie 64-bit. Safe to re-run: every step skips what is already in place.
#   scripts/setup_pi.sh
# HailoRT 5.3.0 is not in Raspberry Pi's apt (that has 5.1.1). When the Pi does not run 5.3.0
# yet, put Hailo's 5.3.0 files in ~/hailo-5.3.0 (or set HAILO_BUNDLE): the hailort and
# hailort-pcie-driver .debs and the cp313 hailort wheel. This script installs them and asks
# for one reboot. Then fill .env (created here) and start with scripts/run_cabinenv.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

HAILO_BUNDLE="${HAILO_BUNDLE:-$HOME/hailo-5.3.0}"
MLN_TAG="core/v0.202610.0"
MLN_PRESET="linux-gnu-arm64-egl"
MLN_SHA256="f4e502b34f1f020a5a229d51cbd49ace075fee884ac0016a87116a55decfe9ad"
MLN_URL="https://github.com/maplibre/maplibre-native-ffi/releases/download/${MLN_TAG/\//%2F}/maplibre-native-c-${MLN_PRESET}.tar.gz"
APT_PKGS=(build-essential cmake ninja-build git curl libsdl2-dev libegl-dev libgles-dev libportaudio2)
REQUIRED_KEYS=(GROQ_API_KEY MAPBOX_API_KEY)
OPTIONAL_KEYS=(EXA_API_KEY OPENROUTER_API_KEY TAVILY_API_KEY)

step() { printf '\n== %s\n' "$*"; }
die() { printf '\nsetup_pi: %s\n' "$*" >&2; exit 1; }
hailo_fw() { hailortcli fw-control identify 2>/dev/null | sed -n 's/^Firmware Version: \([0-9.]*\).*/\1/p' | head -1; }

step "1/8 Checking the Pi"
[[ "$(uname -m)" == aarch64 ]] || die "run this on the Raspberry Pi, not on $(uname -m)."
py="$(/usr/bin/python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
[[ "$py" == 3.13 ]] || die "Python $py found; this needs Raspberry Pi OS Trixie (Python 3.13), 64-bit."
if ! grep -q wm8960 /proc/asound/cards 2>/dev/null; then
  echo "note: no WM8960 Audio HAT sound card. With that HAT, install its driver once and reboot:"
  echo "  git clone https://github.com/waveshare/WM8960-Audio-HAT && cd WM8960-Audio-HAT && sudo ./install.sh && sudo reboot"
  echo "(another mic and speaker works too: pick them as the default in the desktop's sound settings)"
fi

step "2/8 HailoRT 5.3.0"
fw="$(command -v hailortcli >/dev/null && hailo_fw || true)"
if [[ "$fw" == 5.3.* ]] && /usr/bin/python3 -c 'import hailo_platform' 2>/dev/null; then
  echo "Hailo-10H firmware $fw, hailo_platform OK"
else
  deb_rt=("$HAILO_BUNDLE"/hailort_5.3.0_arm64.deb)
  deb_drv=("$HAILO_BUNDLE"/hailort-pcie-driver_5.3.0_all.deb)
  whl=("$HAILO_BUNDLE"/hailort-5.3.0-cp313-*.whl)
  [[ -f "${deb_rt[0]}" && -f "${deb_drv[0]}" && -f "${whl[0]}" ]] || die \
"this Pi runs Hailo firmware '${fw:-none}', and 5.3.0 is needed.
Copy the HailoRT 5.3.0 files into $HAILO_BUNDLE (hailort_5.3.0_arm64.deb,
hailort-pcie-driver_5.3.0_all.deb, hailort-5.3.0-cp313-*.whl) and run this again."
  if dpkg -s hailo-h10-all >/dev/null 2>&1 || dpkg -s hailo-all >/dev/null 2>&1; then
    die "Raspberry Pi's older Hailo packages are installed. Remove them first:
  sudo apt remove hailo-h10-all hailo-all && sudo apt autoremove
then run this again."
  fi
  echo "installing HailoRT 5.3.0 from $HAILO_BUNDLE (asks for your password)"
  sudo apt-get update -qq
  sudo apt-get install -y dkms linux-headers-rpi-2712
  # The PCIe driver is compiled for the running kernel. A Pi that has not been upgraded runs an
  # older kernel than the newest headers, and the build fails inside dpkg with no clear reason.
  if [[ ! -d "/lib/modules/$(uname -r)/build" ]]; then
    die "the running kernel $(uname -r) has no headers to build the Hailo driver against.
Bring the kernel up to date, reboot, and run this again:
  sudo apt update && sudo apt full-upgrade -y && sudo reboot"
  fi
  # Copied out of the home folder so apt's sandbox user can read them.
  stage="$(mktemp -d)"
  cp "${deb_drv[0]}" "${deb_rt[0]}" "$stage/" && chmod 755 "$stage" && chmod 644 "$stage"/*.deb
  sudo apt-get install -y "$stage/$(basename "${deb_drv[0]}")" "$stage/$(basename "${deb_rt[0]}")"
  sudo /usr/bin/python3 -m pip install --break-system-packages "${whl[0]}"
  printf '\nHailoRT 5.3.0 is installed. Reboot the Pi (sudo reboot), then run scripts/setup_pi.sh again.\n'
  exit 0
fi

step "3/8 System packages"
missing=()
for p in "${APT_PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "installing: ${missing[*]} (asks for your password)"
  sudo apt-get update -qq && sudo apt-get install -y "${missing[@]}"
else
  echo "all present"
fi

# Wi-Fi power saving parks the radio between packets: latency swings from 3 ms to 200+ ms, the cockpit's car
# feed arrives in bursts and voice links drop. Off now, and in NetworkManager so it stays off after a reboot.
if command -v iw >/dev/null && iw dev wlan0 get power_save 2>/dev/null | grep -q "on"; then
  echo "turning off Wi-Fi power saving (asks for your password)"
  sudo iw dev wlan0 set power_save off || true
  con="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: '$2 == "wlan0" {print $1; exit}')"
  [[ -n "$con" ]] && sudo nmcli connection modify "$con" 802-11-wireless.powersave 2 || true
fi

# The reSpeaker XVF3800 reports speech energy after its echo canceller over USB; Nova reads it
# for barge-in (nova_hailo/xvf3800.py). Its device node is root-only without this rule.
XVF_RULE=/etc/udev/rules.d/99-respeaker-xvf3800.rules
if [[ ! -f "$XVF_RULE" ]]; then
  echo "letting the plugdev group read the reSpeaker XVF3800 over USB (asks for your password)"
  echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="001a", MODE="0660", GROUP="plugdev"' \
    | sudo tee "$XVF_RULE" >/dev/null
  sudo udevadm control --reload-rules && sudo udevadm trigger --attr-match=idVendor=2886 || true
  id -nG | grep -qw plugdev || sudo usermod -aG plugdev "$USER"
fi

# A USB GPS goes through gpsd: it owns the port and shares the fix with Nova, cgps and xgps
# (Nova finds gpsd by itself). Only with a receiver plugged in; an existing DEVICES line is kept.
gps_port="$(ls /dev/serial/by-id/* 2>/dev/null | head -1)"
if [[ -n "$gps_port" ]]; then
  if ! dpkg -s gpsd >/dev/null 2>&1; then
    echo "installing gpsd for the GPS on $gps_port (asks for your password)"
    sudo apt-get install -y gpsd gpsd-clients
  fi
  if ! grep -q '^DEVICES="/dev' /etc/default/gpsd 2>/dev/null; then
    printf 'START_DAEMON="true"\nUSBAUTO="true"\nDEVICES="%s"\nGPSD_OPTIONS="-n"\n' "$gps_port" \
      | sudo tee /etc/default/gpsd >/dev/null
  fi
  sudo systemctl enable --now gpsd.socket gpsd >/dev/null 2>&1 || true
fi

step "4/8 Python environment (.venv)"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
# System Python with system site-packages: hailo_platform is a system package, not on PyPI.
[[ -x .venv/bin/python ]] || uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -q -e ".[typesafe]"
.venv/bin/python -c 'import hailo_platform, nova_hailo' || die "the venv cannot see hailo_platform; delete .venv and run this again."

step "5/8 Speech and language models (the first run downloads ~2.5 GB and builds two libraries)"
# The demo runs the Instruct HEF; the 3 GB function-calling HEF is opt-in (HAILO_FETCH_FC_HEF=1).
HAILO_FETCH_FC_HEF="${HAILO_FETCH_FC_HEF:-0}" ./scripts/fetch_models.sh

step "6/8 Map engine (MapLibre Native, prebuilt)"
mln_dir="cb_lvgl/.deps/maplibre/$MLN_PRESET"
if [[ -f "$mln_dir/maplibre-native-c-$MLN_PRESET/include/maplibre_native_c.h" ]]; then
  echo "already in $mln_dir"
else
  mkdir -p "$mln_dir"
  tgz="cb_lvgl/.deps/maplibre/maplibre-native-c-$MLN_PRESET.tar.gz"
  curl -fL --retry 3 -o "$tgz" "$MLN_URL"
  echo "$MLN_SHA256  $tgz" | sha256sum -c --quiet || { rm -f "$tgz"; die "the MapLibre download is corrupt; run this again."; }
  tar -xzf "$tgz" -C "$mln_dir"
fi

step "7/8 Cockpit display (cb_lvgl)"
cmake -S cb_lvgl -B cb_lvgl/build-sw -G Ninja -DCB_RENDER=sw -DCMAKE_BUILD_TYPE=Release >/tmp/cb_lvgl_cmake.log 2>&1 \
  || { tail -20 /tmp/cb_lvgl_cmake.log; die "cockpit configure failed (log /tmp/cb_lvgl_cmake.log)."; }
grep -q "Map: MapLibre Native at" /tmp/cb_lvgl_cmake.log || echo "warning: map engine not found; the map page will show a placeholder"
ninja -C cb_lvgl/build-sw cb_lvgl >/tmp/cb_lvgl_build.log 2>&1 \
  || { grep -E "error" /tmp/cb_lvgl_build.log | head -20; die "cockpit build failed (log /tmp/cb_lvgl_build.log)."; }
echo "built cb_lvgl/build-sw/cb_lvgl"

step "8/8 API keys (.env)"
[[ -f .env ]] || { cp .env.example .env; echo "created .env from .env.example"; }
has_key() { grep -qE "^$1=.+" .env; }   # checks names only; values are never printed
need=(); for k in "${REQUIRED_KEYS[@]}"; do has_key "$k" || need+=("$k"); done
opt=(); for k in "${OPTIONAL_KEYS[@]}"; do has_key "$k" || opt+=("$k"); done
if ((${#opt[@]})); then echo "optional, not set: ${opt[*]} (web search, cloud fallback, research)"; fi
# The cloud model the demo is tuned on (a display's Local/Cloud switch saves the same file).
# Kept when it exists, so a choice made on the display survives a re-run.
if [[ ! -f runtime/hmi_settings.json ]]; then
  failover=False; has_key OPENROUTER_API_KEY && failover=True
  .venv/bin/python -c "from nova_hailo.settings_store import save_settings
save_settings({'mode': 'openrouter', 'cloud_provider': 'groq', 'or_model': 'openai/gpt-oss-120b', 'failover': $failover})"
  echo "language model: Groq gpt-oss-120b (cloud), failover to OpenRouter: $failover"
elif ! grep -q '"mode": "openrouter"' runtime/hmi_settings.json; then
  echo "language model: the local Hailo model (saved earlier). For the cloud model the demo is tuned on,"
  echo "  delete runtime/hmi_settings.json and run this again."
fi
if ((${#need[@]})); then
  printf '\nSetup is done except the keys. Open .env and fill in: %s\nThen start the demo: scripts/run_cabinenv.sh\n' "${need[*]}"
  exit 0
fi
printf '\nAll set. Start the demo: scripts/run_cabinenv.sh\n'
