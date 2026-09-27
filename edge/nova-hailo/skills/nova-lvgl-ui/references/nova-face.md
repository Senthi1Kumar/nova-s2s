# The default Nova screen ("Nova face")

Implemented in `examples/realtime/lvgl/src/nova_face.c`. Keep this look unless the
user asks for changes.

## Elements

| Element | Behaviour |
|---|---|
| Orb (centre) | IDLE: dim, slow breathing (opacity 90→180, 2 s, ease in-out, repeating). LISTENING: size follows `level` (up to +25%). THINKING: a 70° arc spins around it (1 s per turn). SPEAKING: a ring ripples out to 1.5× and fades (0.9 s). |
| Connection dot | connecting: amber · up: green · down: red; down also returns the orb to IDLE |
| Subtitles | `You: …` (one line, muted) and Nova's reply (two lines, bright); longer text ends in "…" |
| Tap-to-talk button | Sends `{"wake":true}` (or calls the wake callback) |
| Error banner | Hidden when empty. `session_limit_reached` → "Another screen is using Nova"; `deny_bad_token` → "Wrong API token"; `deny_no_token_configured` → "Nova needs an API token"; anything else → the type as-is |

## Palette

| Use | Hex |
|---|---|
| Background | `#0B0F14` |
| Text / muted text | `#E6EDF3` / `#8B949E` |
| IDLE / LISTENING / THINKING / SPEAKING | `#3A4A5C` / `#2F81F7` / `#A371F7` / `#3FB950` |
| Connecting / error | `#D29922` / `#F85149` |

## Layout

`side` = the smaller of width and height; orb = 26% of `side`.

- **Square and round panels** (aspect ≤ 1.3:1): orb above centre (offset −h/8);
  the text column (subtitles over the button) is 60% wide, sized to its content, and
  ends at 85% of the height; on panels under 300 px the "You:" line is hidden so the
  column clears the orb. Dot at 8% and banner at 12% from the top, both centred. On a
  round panel the inscribed circle is ≈71% wide at 85% height, so the 60% column always fits.
- **Wide panels** (aspect > 1.3:1, e.g. 1024×600): orb on the left (w/10 in), text
  column on the right at 50% width.
- Fonts: Montserrat 20 when `side` ≥ 400, otherwise Montserrat 14.

## Porting rules

- Call `nova_face_*` only on the LVGL thread: LVGL is not thread-safe, and `lv_lock()`/
  `lv_unlock()` only lock when `LV_USE_OS` is set (the default `LV_OS_NONE` makes them
  no-ops), so on ESP32 wrap calls in `lvgl_port_lock(0)`/`lvgl_port_unlock()` (Espressif's
  `esp_lvgl_port`) instead.
- Setters do nothing before `nova_face_create`, so a client may send state early.
- `nova_face_press_wake()` is the tap action; map a physical button to it.
