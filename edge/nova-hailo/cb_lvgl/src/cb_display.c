#include "cb_display.h"
#include "cb_theme.h"
#include "map/map_view.h"
#include "screens/cockpit.h"
#include <SDL2/SDL.h>

static lv_obj_t *s_panel;
static lv_group_t *s_group;

/* Wheel zoom like a web map: LVGL hands the wheel to the last pressed object, so watch SDL directly and give
   it to the map under the cursor. Runs on the main thread inside LVGL's SDL event pump. */
static int wheel_watch(void *ud, SDL_Event *e)
{
    LV_UNUSED(ud);
    if(e->type != SDL_MOUSEWHEEL || !cockpit()->map) return 1;
    int x, y;
    SDL_GetMouseState(&x, &y);
    float dy = e->wheel.preciseY != 0 ? e->wheel.preciseY : (float)e->wheel.y;
    if(e->wheel.direction == SDL_MOUSEWHEEL_FLIPPED) dy = -dy;
    map_view_wheel(cockpit()->map, x, y, dy);
    return 1;
}

lv_display_t *cb_display_create(const cb_display_cfg_t *cfg)
{
    /* The map renders through its own EGL context on this thread; with libglvnd a GLX context current here makes
       every eglMakeCurrent fail (EGL_BAD_ACCESS), so SDL's renderer must use EGL on X11 too. */
    SDL_SetHint(SDL_HINT_VIDEO_X11_FORCE_EGL, "1");
    lv_display_t *disp = lv_sdl_window_create(cfg->w, cfg->h);
    if(!disp) return NULL;
    lv_sdl_window_set_title(disp, "Nova Cabin");
    lv_sdl_window_set_resizeable(disp, true);
    if(cfg->fullscreen) SDL_SetWindowFullscreen(lv_sdl_window_get_window(disp), SDL_WINDOW_FULLSCREEN_DESKTOP);

    lv_indev_t *mouse = lv_sdl_mouse_create();
    lv_indev_set_display(mouse, disp);
    lv_indev_t *kb = lv_sdl_keyboard_create();
    lv_indev_set_display(kb, disp);
    /* LVGL's SDL keyboard reads letters from SDL_TEXTINPUT only. SDL2 starts text input by itself;
       sdl2-compat (SDL3 underneath, e.g. Arch) does not, which silently drops the letter shortcuts. */
    SDL_StartTextInput();
    SDL_AddEventWatch(wheel_watch, NULL);
    s_group = lv_group_create();
    lv_indev_set_group(kb, s_group);

    s_panel = cb_panel_create(lv_display_get_screen_active(disp));
    return disp;
}

lv_obj_t *cb_display_panel(void) { return s_panel; }

lv_group_t *cb_display_key_group(void) { return s_group; }

const char *cb_display_renderer_name(void) { return CB_RENDER_GPU ? "gpu" : "sw"; }

const char *cb_display_video_driver(void)
{
    const char *d = SDL_GetCurrentVideoDriver();
    return d ? d : "?";
}
