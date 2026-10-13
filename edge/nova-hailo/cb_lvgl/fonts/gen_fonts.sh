#!/usr/bin/env bash
# Generate cb_lvgl's bitmap fonts (Inter + JetBrains Mono, both SIL OFL 1.1).
# Run on the laptop; the generated fonts/cb_*.c are committed so the Pi needs no Node.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CACHE=${CB_FONT_CACHE:-$HOME/.cache/cb_lvgl/fonts}
mkdir -p "$CACHE"

fetch() {  # url zipname dir
  if [ ! -d "$CACHE/$3" ]; then
    curl -sSL -o "$CACHE/$2" "$1"
    mkdir -p "$CACHE/$3"
    (cd "$CACHE/$3" && unzip -q "../$2")
  fi
}
fetch https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip inter.zip inter
fetch https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip jbmono.zip jbmono

ttf() { find "$CACHE/$1" -name "$2" -print -quit; }
RANGE=0x20-0x7E,0xB0,0xB7,0xD7,0x2014,0x2019,0x201C,0x201D,0x2026,0x2190,0x2192,0x2212

gen() {  # name ttf size dir
  local src; src=$(ttf "$4" "$2")
  [ -n "$src" ] || { echo "missing $2" >&2; exit 1; }
  npx -y lv_font_conv@1.5.3 --bpp 4 --format lvgl --no-compress --lv-include lvgl.h \
    --font "$src" --size "$3" --range "$RANGE" --lv-font-name "$1" -o "$HERE/$1.c"
  echo "$1 <- $(basename "$src") @ $3"
}

gen cb_inter_11 Inter-Regular.ttf  11 inter
gen cb_inter_12 Inter-Medium.ttf   12 inter
gen cb_inter_14 Inter-Medium.ttf   14 inter
gen cb_inter_16 Inter-SemiBold.ttf 16 inter
gen cb_inter_20 Inter-SemiBold.ttf 20 inter
gen cb_inter_34 Inter-SemiBold.ttf 34 inter
gen cb_inter_52 Inter-SemiBold.ttf 52 inter
gen cb_mono_11  JetBrainsMono-Regular.ttf 11 jbmono
gen cb_mono_12  JetBrainsMono-Regular.ttf 12 jbmono
gen cb_mono_14  JetBrainsMono-Medium.ttf  14 jbmono
