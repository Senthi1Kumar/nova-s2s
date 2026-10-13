#pragma once
#include "lvgl.h"

/* Render `obj` off-screen and write it as a binary PPM (P6). Dev/test aid for visual checks;
   scripts/ppm2png.py converts the result. Returns true on success. */
bool cb_snapshot_ppm(lv_obj_t *obj, const char *path);

/* Take one snapshot of `obj` after `delay_ms` (timer-driven, for the running app). */
void cb_snapshot_after(lv_obj_t *obj, const char *path, uint32_t delay_ms);
