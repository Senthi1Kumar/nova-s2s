#include "display.h"
#include "theme.h"
#include <SDL2/SDL.h>

static lv_obj_t *s_root;
static lv_group_t *s_group;
static lv_group_t *s_wheel_group;

lv_group_t *cb_display_wheel_group(void) { return s_wheel_group; }

lv_display_t *cb_display_create(const cb_display_cfg_t *cfg)
{
    SDL_SetHint(SDL_HINT_VIDEO_X11_FORCE_EGL, "1");
    lv_display_t *disp = lv_sdl_window_create(cfg->w, cfg->h);
    if(!disp) return NULL;
    lv_sdl_window_set_title(disp, "AI Box Studio");
    lv_sdl_window_set_resizeable(disp, true);
    if(cfg->fullscreen) SDL_SetWindowFullscreen(lv_sdl_window_get_window(disp), SDL_WINDOW_FULLSCREEN_DESKTOP);

    lv_indev_t *mouse = lv_sdl_mouse_create();
    lv_indev_set_display(mouse, disp);
    lv_indev_t *kb = lv_sdl_keyboard_create();
    lv_indev_set_display(kb, disp);
    /* sdl2-compat (SDL3 underneath) does not start text input by itself. */
    SDL_StartTextInput();
    s_group = lv_group_create();
    lv_indev_set_group(kb, s_group);
    /* The SDL wheel is an encoder; its own group keeps it from moving keyboard focus. */
    lv_indev_t *wheel = lv_sdl_mousewheel_create();
    lv_indev_set_display(wheel, disp);
    s_wheel_group = lv_group_create();
    lv_indev_set_group(wheel, s_wheel_group);

    lv_obj_t *scr = lv_display_get_screen_active(disp);
    lv_obj_set_style_bg_color(scr, cb_col(CB_BASE), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(scr, false);
    s_root = lv_obj_create(scr);
    lv_obj_remove_style_all(s_root);
    lv_obj_set_size(s_root, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_root, cb_col(CB_BASE), 0);
    lv_obj_set_style_bg_opa(s_root, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(s_root, false);
    return disp;
}

lv_obj_t *cb_display_root(void) { return s_root; }

lv_group_t *cb_display_key_group(void) { return s_group; }

const char *cb_display_renderer_name(void) { return CB_RENDER_GPU ? "gpu" : "sw"; }

const char *cb_display_video_driver(void)
{
    const char *d = SDL_GetCurrentVideoDriver();
    return d ? d : "?";
}
