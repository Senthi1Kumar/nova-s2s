# LVGL docs map (fetch as `https://lvgl.io/docs/open/llms.mdx/<path>`)

Index: `https://lvgl.io/docs/open/llms.txt`. Never fetch `llms-full.txt`.
The old `https://docs.lvgl.io/` URLs redirect to `https://lvgl.io/docs/open/`; if a
fetch reports a redirect to another host, follow it.
Paths were checked against the live index; if one 404s, search the index for its title.

| Task / target | Pages |
|---|---|
| First contact with LVGL | `/getting_started/learn_the_basics` |
| Build with CMake | `/integration/building/cmake` |
| Desktop window (Linux, macOS, Pi desktop) | `/integration/pc/sdl` |
| Pi desktop, native | `/integration/embedded_linux/drivers/wayland` |
| Pi kiosk (no desktop) | `/integration/embedded_linux/drivers/drm`, `/integration/embedded_linux/drivers/fbdev` |
| ESP32 + ESP-IDF | `/integration/chip_vendors/espressif/overview`, `/integration/chip_vendors/espressif/add_lvgl_to_esp32_idf_project`, `/integration/chip_vendors/espressif/tips_and_tricks` |
| Display setup, buffers, rotation | `/main-modules/display/setup`, `/main-modules/display/rotation`, `/main-modules/display/color_format` |
| Touch and buttons | `/main-modules/indev/pointer`, `/main-modules/indev/button` |
| Widgets used by the face | `/widgets/arc`, `/widgets/label`, `/widgets/button` |
| Styles | `/common-widget-features/styles/overview`, `/common-widget-features/styles/style-properties` |
| Layout | `/common-widget-features/layouts/flex` |
| Animations | `/main-modules/animation` |
| Fonts | `/main-modules/fonts/overview`, `/main-modules/fonts/built_in_fonts` |

If the docs are unreachable, these v9 names are the ones the face uses:
`lv_display_create`, `lv_display_set_buffers`, `lv_display_set_flush_cb`,
`lv_button_create`, `lv_label_set_long_mode(label, LV_LABEL_LONG_MODE_DOTS)`,
`lv_anim_set_duration`, `lv_anim_set_reverse_duration`, `LV_ANIM_REPEAT_INFINITE`,
`lv_obj_set_hidden`, `lv_obj_set_clickable`, `lv_obj_set_scrollable`, `lv_screen_active`,
`lv_sdl_window_create(w, h)`, `lv_sdl_mouse_create()`.
