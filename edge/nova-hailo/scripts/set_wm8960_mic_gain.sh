#!/usr/bin/env bash
# Set WM8960 capture gain + ALC for ASR capture.
#
# `alsactl store` needs a root password we do not have non-interactively, so
# none of this survives a reboot. Run it at every demo start instead.
#
# ALC matters more than raw gain: input boost is already +29 dB, so a fixed 100%
# PGA clips near-field speech while still under-serving far-field. ALC adjusts
# per utterance, which is what one speaker at two distances needs.
set -euo pipefail

PCT="${1:-100}"
ALC_MODE="${WM8960_ALC:-Stereo}"       # Off | Left | Right | Stereo
ALC_TARGET="${WM8960_ALC_TARGET:-10}"  # 0-15; the chip default of 4 is too quiet far-field

# Resolve by name: the card index moves when other sound devices enumerate first.
CARD="${WM8960_CARD:-}"
if [[ -z "$CARD" ]]; then
  for c in wm8960soundcard 2 1 0; do
    if amixer -c "$c" sget Capture >/dev/null 2>&1; then CARD="$c"; break; fi
  done
fi

if [[ -z "$CARD" ]] || ! amixer -c "$CARD" sget Capture >/dev/null 2>&1; then
  echo "[mic] no WM8960 Capture control found - leaving audio untouched" >&2
  exit 0   # non-fatal: never block the demo on a mixer tweak
fi

# Takes the control name then any number of amixer value args. They must stay
# separate words: `sset Capture "100% unmute"` is one token and amixer rejects it.
set_ctl() {
  local ctl="$1"; shift
  if ! amixer -c "$CARD" sset "$ctl" "$@" >/dev/null 2>&1; then
    echo "[mic] warn: could not set '$ctl' to '$*'" >&2
  fi
}

set_ctl Capture "${PCT}%" unmute
set_ctl 'Left Input Boost Mixer LINPUT1' 3
set_ctl 'Right Input Boost Mixer RINPUT1' 3
set_ctl 'ALC Function' "$ALC_MODE"
set_ctl 'ALC Target' "$ALC_TARGET"
set_ctl 'ALC Max Gain' 7
# Drops sub-80 Hz road rumble; well below the ~85-180 Hz male fundamental.
set_ctl 'ADC High Pass Filter' on

cap=$(amixer -c "$CARD" sget Capture 2>/dev/null | tail -1 | sed 's/^ *//')
alc=$(amixer -c "$CARD" sget 'ALC Function' 2>/dev/null | tail -1 | sed 's/^ *//')
echo "[mic] card=$CARD  $cap"
echo "[mic] ALC=$alc  target=$ALC_TARGET  HPF=on"
