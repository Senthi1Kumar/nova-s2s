#!/usr/bin/env bash
# Set reSpeaker XVF3800 playback and capture levels for Nova.
#
# The card's mixer comes up at -20 dB playback, which is barely audible on the
# small JST speaker, and /var/lib/alsa/asound.state on the demo Pi still points
# at the old WM8960 state, so nothing is restored at boot. Run at every start.
# Non-fatal: exits 0 when no XVF3800 is plugged in.
set -euo pipefail

PLAY_PCT="${1:-${XVF3800_PLAYBACK_PCT:-100}}"
CAP_PCT="${XVF3800_CAPTURE_PCT:-100}"

# The card index moves with enumeration order; its ALSA id is "Array".
CARD="${XVF3800_CARD:-}"
if [[ -z "$CARD" ]] && grep -qi "XVF3800" /proc/asound/cards 2>/dev/null; then
  CARD=$(awk '/XVF3800/ { print $1; exit }' /proc/asound/cards)
fi
if [[ -z "$CARD" ]]; then
  echo "[xvf3800] not plugged in - leaving audio untouched" >&2
  exit 0
fi

amixer -q -c "$CARD" cset name='PCM Playback Switch' on,on 2>/dev/null || true
amixer -q -c "$CARD" cset name='PCM Playback Volume' "${PLAY_PCT}%,${PLAY_PCT}%" 2>/dev/null || true
amixer -q -c "$CARD" cset name='Headset Capture Switch' on,on 2>/dev/null || true
amixer -q -c "$CARD" cset name='Headset Capture Volume' "${CAP_PCT}%,${CAP_PCT}%" 2>/dev/null || true
echo "[xvf3800] card $CARD playback ${PLAY_PCT}% capture ${CAP_PCT}% (echo cancelled on the board)"
