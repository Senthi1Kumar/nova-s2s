# Nova face — LVGL v9 reference screen

Renders the UI-state contract (`docs/api/ui-state.md`): an orb per state,
subtitles, a connection dot, a tap-to-talk button and an error banner. It only
draws; the connection belongs to the client.

- `src/nova_face.c/.h` — the portable part (LVGL only). Reuse it on any target.
- `src/main.c` — the desktop shell: SDL window + JSON lines on stdin.

## Build (Linux desktop or Raspberry Pi)

Needs a C compiler, CMake ≥ 3.24 and SDL2 development files
(`sudo apt install libsdl2-dev` / `pacman -S sdl2-compat`). LVGL v9.6.0 and
cJSON v1.7.19 are fetched by CMake.

```bash
cmake -S examples/realtime/lvgl -B examples/realtime/lvgl/build
cmake --build examples/realtime/lvgl/build -j
```

## Run

```bash
B=examples/realtime/lvgl/build/nova_face
$B --demo                                  # cycles through the states
$B --size 360x360 --round --demo           # preview a round panel
$B --size 1024x600 --demo                  # wide panel layout
$B --headless --script examples/realtime/lvgl/demo/states.jsonl   # no window; exits 0
$B --headless --script examples/realtime/lvgl/demo/states.jsonl --dump   # test/debug: print the widget tree as JSON lines and exit

# With a real Nova conversation (the client starts the face and feeds it):
python examples/realtime/python/nova_client.py --url wss://<pi>:8767/v1/realtime \
  --token "$NOVA_HAILO_API_TOKEN" --ca cert.pem --wake device --ui "$B"
```

On the Pi's desktop over SSH, run inside the Wayland session:
`WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) $B --demo`.

## Porting

Keep `nova_face.c`. Replace `main.c` with your platform's LVGL display and
input setup (see `skills/nova-lvgl-ui/references/lvgl-docs-map.md`), and call
the `nova_face_set_*` functions from your realtime event handler on the LVGL
thread. A hardware button can call `nova_face_press_wake()`.
