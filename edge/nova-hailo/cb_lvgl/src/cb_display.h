#pragma once
#include "lvgl.h"

/* The only module that knows about SDL/EGL. Everything else builds into cb_display_panel(). */
typedef struct {
    int32_t w, h;      /* window size; the panel stays 1920x720 and is centred inside */
    bool fullscreen;   /* SDL desktop fullscreen */
} cb_display_cfg_t;

lv_display_t *cb_display_create(const cb_display_cfg_t *cfg);
lv_obj_t     *cb_display_panel(void);          /* 1920x720 root, centred on the active screen */
lv_group_t   *cb_display_key_group(void);      /* keyboard focus group; main adds its key sink here */
const char   *cb_display_renderer_name(void);  /* "gpu" | "sw" */
const char   *cb_display_video_driver(void);   /* SDL's current video driver, e.g. "x11", "wayland" */
