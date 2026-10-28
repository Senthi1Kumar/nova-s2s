#pragma once
#include "lvgl.h"

/* The only module that knows about SDL/EGL. The root object fills the whole display. */
typedef struct {
    int32_t w, h;      /* window size; 0 = pick (fullscreen: the desktop size) */
    bool fullscreen;   /* SDL desktop fullscreen */
} cb_display_cfg_t;

lv_display_t *cb_display_create(const cb_display_cfg_t *cfg);
lv_obj_t     *cb_display_root(void);           /* full-screen root on the active screen */
lv_group_t   *cb_display_key_group(void);
lv_group_t   *cb_display_wheel_group(void);  /* the mouse wheel's encoder group */
const char   *cb_display_renderer_name(void);  /* "gpu" | "sw" */
const char   *cb_display_video_driver(void);
