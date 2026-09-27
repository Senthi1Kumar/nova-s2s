---
name: nova-lvgl-ui
description: Build an LVGL screen for a Nova voice device — ESP32 (ESP-IDF or Arduino), Raspberry Pi (Wayland desktop or DRM/fbdev kiosk), or a Linux/macOS desktop — that shows Nova's state (idle, listening, thinking, speaking), subtitles, a tap-to-talk button and errors. Fetches the current LVGL docs at runtime. Use when someone wants a display, UI, face or touch screen on a device that talks to Nova, or asks for LVGL code for it. Pair with nova-realtime-client, which owns the connection.
---

# Nova LVGL UI

This skill builds **only the screen**. The client (see the `nova-realtime-client`
skill) owns the connection and drives the screen with UI-state updates defined in
`references/ui-state.md`. The proven starting point is `examples/realtime/lvgl/`
("Nova face"): `src/nova_face.c/.h` is portable LVGL code; `src/main.c` is a
desktop shell. Do not start from or copy `hmi_lvgl/` if it is present: it is a
separate, unfinished app.

## 1. Intake

Ask one question at a time; skip what is known.

1. Target: ESP32 + ESP-IDF, ESP32 + Arduino, Linux desktop, Pi desktop (Wayland),
   Pi kiosk (no desktop: DRM or fbdev), other MCU.
2. Display: resolution, **round or rectangular**, colour depth, panel driver chip.
3. Input: touch, physical buttons, none.
4. LVGL version already in the project (default **v9.6.0** for new projects).
5. On MCUs: RAM/PSRAM budget for draw buffers.
6. Screen content: the default (orb + subtitles + tap-to-talk + error banner,
   `references/nova-face.md`) or changes.

## 2. Load LVGL knowledge at runtime

1. Fetch the index `https://lvgl.io/docs/open/llms.txt`.
2. Pick pages with `references/lvgl-docs-map.md` for this target and task; fetch each
   as `https://lvgl.io/docs/open/llms.mdx/<path>`. If a path 404s, search the index for
   the page title.
3. **Never fetch `llms-full.txt`** (8.8 MB) — on either host
   (`https://lvgl.io/docs/open/llms-full.txt`, `https://docs.lvgl.io/llms-full.txt`).
4. The old `https://docs.lvgl.io/` URLs redirect to `https://lvgl.io/docs/open/`; if a
   fetch reports a redirect to another host, follow it.
5. The docs describe the latest LVGL (v9.6). If the project uses **v8**, say so and ask:
   upgrade to v9, or proceed with v8 knowledge carefully (many names differ, e.g.
   `lv_disp_*` → `lv_display_*`, `lv_btn_create` → `lv_button_create`).
6. If the docs are unreachable, work from `examples/realtime/lvgl/` and the notes in
   the map, and tell the user.

## 3. Build order

1. **Desktop first.** Build `examples/realtime/lvgl` (its README) and run
   `--demo`, or `--headless --script demo/states.jsonl`; use `--size WxH --round` to
   preview the target panel. No server needed.
2. **Real client.** `nova_client.py ... --ui examples/realtime/lvgl/build/nova_face`:
   watch idle → listening → thinking → speaking and the subtitles; tap to talk.
3. **Port.** Keep `nova_face.c/.h` unchanged. Replace `main.c` with the target's
   display and input setup from the fetched docs:
   - Linux desktop / Pi desktop: SDL (as `main.c`) or the Wayland driver.
   - Pi kiosk: DRM (or fbdev) driver; the desktop must not be running.
   - ESP32 + ESP-IDF: the Espressif integration pages; call `nova_face_set_*` from the
     realtime event handler on the LVGL task, and map a hardware button to
     `nova_face_press_wake()`. LVGL is not thread-safe: call `nova_face_set_*` on the
     LVGL task, or wrap the calls in `lvgl_port_lock(0)`/`lvgl_port_unlock()` when using
     Espressif's `esp_lvgl_port`. `lv_lock()`/`lv_unlock()` only lock when `LV_USE_OS` is
     set (e.g. `LV_OS_FREERTOS`); with the default `LV_OS_NONE` they do nothing.
4. **Fit the panel** per `references/nova-face.md`: fonts, orb size, round-safe layout.
5. On ESP32, check flash: an LVGL UI plus audio usually needs an app partition
   larger than 1 MB (e.g. `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE=y`).

## 4. Verify

- Desktop: `--headless --script` exits 0; the demo shows every state; round and wide
  previews keep everything on screen.
- With the client: every state change and subtitle appears; a tap sends `wake`
  (the client logs `[wake] local (screen)`); errors show in the banner.
- On the target: the same checks on the real panel.
