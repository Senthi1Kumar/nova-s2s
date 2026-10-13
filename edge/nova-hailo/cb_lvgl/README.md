# cb_lvgl — Nova cabin UI (LVGL v9.6)

The cockpit of CabinEnv: a native LVGL app that shows the simulated (or real-adapter) cabin and Nova, driven
live by the nova-hailo pipeline over `/v1/observe`, with touch writes back through `POST /v1/vehicle/set`.
Default layout **B · car + pages**, full screen: the car pane on the left (speed, climate, devices), Map /
Media / Nova pages on the right, and a dock with temperature −/+, the Nova bar and a mini player. The earlier
1920×720 frame D stays available (`--layout d`). `--layout v2` is the v0.0.2 avatar cockpit: layout B's pages
and dock, with Nova as a small robot in a glass panel above the device tiles (the user's words slide by when long,
answer cards show as a table, a green dot marks a card that arrived while tucked) and tiles that mark a change
with a travelling gradient glow. Runs as an SDL window on the laptop and on the Raspberry Pi 5.
Design: `.docs/CabinGym/2026-10-07-cb-lvgl-design.md`, live data: `.docs/CabinGym/2026-10-09-cb-lvgl-m4-lite-design.md`.

## Where the demo lives

Branch `main` holds both halves: the Nova pipeline in `edge/nova-hailo` (speech, cabin tools, Mapbox place
search, `/v1/observe`) and this cockpit in `edge/nova-hailo/cb_lvgl`. A new Pi follows nova-hailo's
[QUICKSTART.md](../QUICKSTART.md).

Targets that are built and run: an x64 Linux laptop (SDL, software renderer) and a Raspberry Pi 5 (arm64). The `gpu` renderer needs X11. MapLibre packages are per architecture (`linux-gnu-x64-egl`, `linux-gnu-arm64-egl`) and are not committed (`.deps/` is ignored).

## Run the cabin demo

Pipeline on the Pi, cockpit on the laptop. The Pi demo tree is
`~/repos/test_nova_repo/nova-s2s/edge/nova-hailo` (branch `main`). Copy `.env.example` to `.env` on the Pi and set `MAPBOX_API_KEY` (and the LLM key the pipeline already uses). Do not commit `.env`.

```bash
# Pi
scripts/run_demo_oem.sh

# Laptop, from cb_lvgl/ — voice through this machine's mic and speaker
NOVA_LAPTOP_AEC=1 NOVA_HAILO_DIR=/path/to/nova-hailo \
  cb_lvgl/scripts/run_cabinenv_laptop.sh --client
```

`NOVA_HAILO_DIR` must be the pipeline checkout, not this worktree. The script tunnels the Pi's observe port to the laptop and opens `build-sw/cb_lvgl` full screen. Close any other Nova session first. Ctrl-C restores the mic and the echo-cancel devices.

Without `--client`, the same script opens only the cockpit and voice stays in the browser nova-connect page. `CB_LAYOUT=v2` selects the avatar cockpit. `PI` overrides the SSH host (default `RPi5-AI-HAT+2`).

On the Pi itself, after the pipeline is up: `cb_lvgl/scripts/run_on_pi.sh sw --fullscreen --live`.

## Build

Needs CMake ≥ 3.21, Ninja, a C11 compiler, SDL2 dev files, and for `gpu` also EGL + GLESv2 dev files.
LVGL v9.6.0 is fetched by CMake (pinned hash). Two renderers, one build directory each:

```bash
cmake -S . -B build-sw  -G Ninja -DCB_RENDER=sw  && ninja -C build-sw  && ctest --test-dir build-sw
cmake -S . -B build-gpu -G Ninja -DCB_RENDER=gpu && ninja -C build-gpu && ctest --test-dir build-gpu
```

- `sw` (default): LVGL software renderer, any SDL video driver (Wayland or X11). Redraws only what changed,
  which suits this UI: on the Pi it costs ~9 % of one core idle and ~25 % while Nova talks at 60 fps.
- `gpu`: SDL + EGL + LVGL's bundled NanoVG draw unit. LVGL v9.6's SDL EGL path supports **X11 only**; on a
  Wayland desktop it runs through Xwayland (`DISPLAY=:0`, `SDL_VIDEODRIVER=x11`). NanoVG redraws the whole
  screen every frame, and on the Pi it tops out near 30 fps; it adds the bar's backdrop blur.

Motion budget: decorative loops share one 15 Hz ambient clock (`src/cb_ambient.c`); the orb and the bar's
tuck/expand take a 60 fps frame boost only while they move; everything else refreshes at 30 fps (`sw`).

## Map

The Map page renders Mapbox vector tiles natively with MapLibre Native (C API, prebuilt package
`core/v0.202610.0`, `maplibre-native-c-linux-gnu-{x64,arm64}-egl.tar.gz`, unpacked to
`.deps/maplibre/<preset>/`; CMake finds it, otherwise the page shows a placeholder). It draws off-screen in its
own EGL context on a render thread and hands finished frames to LVGL; drag pans with a glide, the mouse wheel,
double tap and +/− zoom, ⌖ recenters and follows the car, ☀/☾ switches night (`navigation-night-v1`: live
traffic, with place names added from the same tiles) and day (`streets-v12`: shops, restaurants, transit). Frames are rendered only while something moves (30 fps touched,
15 fps following, none idle). `MAPBOX_API_KEY` selects `mapbox://styles/mapbox/dark-v11` (the launchers read it
from nova-hailo's `.env` without printing it); without a token the OpenFreeMap dark style is used, and
`CB_MAP_STYLE` overrides both. `build-*/map_probe` measures render + readback on a machine (Pi 5, V3D:
59 fps unpaced, 1200×760).

## Run

```bash
./build-sw/cb_lvgl [--fullscreen] [--size WxH] [--layout b|d|v2] [--live [ws://host:port/v1/observe]]
                   [--replay file.jsonl [--replay-speed X]] [--page 0|1|2] [--map-day] [--quit-after SEC] [--perf]
                   [--gate] [--no-demo] [--reduce-motion] [--snapshot out.ppm [--snapshot-after MS]]
```

- No source flag: simulated cabin with the scripted demo.
- `--live`: the pipeline drives everything (default `ws://127.0.0.1:8766/v1/observe`, the Pi loopback); the
  dock's temperature −/+ go through `POST /v1/vehicle/set` and the voice agent sees them as context. The
  pipeline owns the mic: the mic button, the orb (or avatar) and × ask it through
  `POST /v1/session/{mute,arm,cancel}`, and the bar or panel shows what it answers (`nova.mic`, `nova.fsm`).
- `--replay`: a recorded observe session (`tests/fixtures/observe-session-synthetic.jsonl`), looped.

Layout B fills the window (designed at 1920×1080, tighter below 900 px of height); frame D is drawn at
1920×720 and centred. `--perf` prints LVGL's FPS/CPU line every second. `--snapshot` renders the panel
off-screen to a PPM; `scripts/ppm2png.py out.ppm` converts it.

Keys: Space wake · M mute · T tuck · O orb tap · E end · P pause demo · 1/2/3 pages · Esc quit.

## Raspberry Pi

```bash
scripts/pi_sync.sh                          # rsync + build gpu and sw + ctest on the Pi (not .deps/: on a new
                                            # Pi, nova-hailo's scripts/setup_pi.sh downloads MapLibre)
ssh RPi5-AI-HAT+2 '~/repos/test_nova_repo/nova-s2s/edge/nova-hailo/cb_lvgl/scripts/run_on_pi.sh sw --fullscreen --live'
scripts/cpu_sample.sh <pid> 60              # on the Pi: % of one core
scripts/measure_on_pi.sh sw 60 tag          # on the Pi: run, screenshots, CPU, alive check (EVENTS="6:shot 9:key=m ...")
```

CabinEnv on a new Pi: nova-hailo's [QUICKSTART.md](../QUICKSTART.md) (`scripts/setup_pi.sh` once, then
`scripts/run_cabinenv.sh`). By hand, two terminals in the nova-hailo directory: `scripts/run_demo_oem.sh` (the
pipeline; its `wake.mode` decides "Hey Nova" vs open mic), then `scripts/run_cabinenv_client.sh`, which runs the
headless mic/speaker client and opens this cockpit beside it (`run_on_pi.sh sw --fullscreen --live`, log
`/tmp/cb_lvgl.log`). `run_on_pi.sh` loads `MAPBOX_API_KEY`; the clock follows the car's time zone, which the
pipeline sends (the GPS fix's, else the simulated drive's). In live mode the display is
an observer: mic, × and orb taps do nothing (the pipeline owns the mic); tuck still works.

## Fonts

`fonts/gen_fonts.sh` (laptop, needs Node) downloads Inter 4.1 and JetBrains Mono 2.304 (SIL OFL 1.1) and
regenerates `fonts/cb_*.c`. The generated files are committed. `scripts/make_lv_conf.py` regenerates
`lv_conf.h` from LVGL's v9.6 template plus our overrides.
