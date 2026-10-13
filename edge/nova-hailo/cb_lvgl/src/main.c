#include "lvgl.h"
#include "cb_app.h"
#include "cb_display.h"
#include "cb_snapshot.h"
#include "cb_theme.h"
#include "map/map_view.h"
#include "screens/cockpit.h"
#include "screens/gate.h"
#include "widgets/settings_sheet.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    cb_display_cfg_t disp;
    uint32_t quit_after_s;
    bool perf, gate, demo, reduce_motion;
    const char *snapshot;
    uint32_t snapshot_after_ms;
    cb_layout_t layout;
    const char *live_url, *replay;
    double replay_speed;
    int page;
    bool map_day, settings;
} cb_args_t;

static void usage(const char *argv0)
{
    fprintf(stderr, "usage: %s [--fullscreen] [--size WxH] [--quit-after SEC] [--perf] [--gate] "
                    "[--no-demo] [--reduce-motion] [--layout b|d|v2] [--live [ws://host:port/v1/observe]] [--replay file.jsonl [--replay-speed X]] [--page 0|1|2] [--map-day] [--settings] [--snapshot out.ppm [--snapshot-after MS]]\n", argv0);
}

static bool parse_args(int argc, char **argv, cb_args_t *a)
{
    *a = (cb_args_t){ .disp = { 0, 0, false }, .demo = true, .snapshot_after_ms = 1500, .layout = CB_LAYOUT_B };
    for(int i = 1; i < argc; i++) {
        const char *s = argv[i];
        if(!strcmp(s, "--fullscreen")) a->disp.fullscreen = true;
        else if(!strcmp(s, "--size") && i + 1 < argc) {
            int w, h;
            if(sscanf(argv[++i], "%dx%d", &w, &h) != 2 || w < 320 || h < 240) return false;
            a->disp.w = w;
            a->disp.h = h;
        }
        else if(!strcmp(s, "--live")) {
            a->live_url = (i + 1 < argc && !strncmp(argv[i + 1], "ws://", 5)) ? argv[++i] : "ws://127.0.0.1:8766/v1/observe";
        }
        else if(!strcmp(s, "--replay") && i + 1 < argc) a->replay = argv[++i];
        else if(!strcmp(s, "--page") && i + 1 < argc) a->page = atoi(argv[++i]);
        else if(!strcmp(s, "--map-day")) a->map_day = true;
        else if(!strcmp(s, "--settings")) a->settings = true;
        else if(!strcmp(s, "--replay-speed") && i + 1 < argc) a->replay_speed = strtod(argv[++i], NULL);
        else if(!strcmp(s, "--layout") && i + 1 < argc) {
            const char *l = argv[++i];
            if(!strcmp(l, "b")) a->layout = CB_LAYOUT_B;
            else if(!strcmp(l, "d")) a->layout = CB_LAYOUT_D;
            else if(!strcmp(l, "v2")) a->layout = CB_LAYOUT_V2;
            else return false;
        }
        else if(!strcmp(s, "--quit-after") && i + 1 < argc) a->quit_after_s = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if(!strcmp(s, "--perf")) a->perf = true;
        else if(!strcmp(s, "--gate")) a->gate = true;
        else if(!strcmp(s, "--no-demo")) a->demo = false;
        else if(!strcmp(s, "--reduce-motion")) a->reduce_motion = true;
        else if(!strcmp(s, "--snapshot") && i + 1 < argc) a->snapshot = argv[++i];
        else if(!strcmp(s, "--snapshot-after") && i + 1 < argc) a->snapshot_after_ms = (uint32_t)strtoul(argv[++i], NULL, 10);
        else return false;
    }
    return true;
}

static bool s_quit;

/* --map-day switches after start-up, through the same path as the button (style change at runtime) */
static void settings_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    settings_sheet_open();
}

static void map_day_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    if(cockpit()->map) map_view_set_night(cockpit()->map, false);
}

/* Keyboard: Space wake · M mute · T tuck · O orb tap · E end · P pause demo · 1/2/3 pages · Esc quit. */
static void key_cb(lv_event_t *e)
{
    uint32_t k = lv_event_get_key(e);
    switch(k) {
        case ' ': cb_app_event(NV_EV_WAKE_KWS, NULL); break;
        case 'm': case 'M': cb_app_event(NV_EV_TAP_MIC, NULL); break;
        case 't': case 'T': cb_app_event(NV_EV_TAP_TUCK, NULL); break;
        case 'o': case 'O': cb_app_event(NV_EV_TAP_ORB, NULL); break;
        case 'e': case 'E': cb_app_event(NV_EV_TAP_END, NULL); break;
        case 'p': case 'P': cb_app_toggle_demo_pause(); break;
        case '1': case '2': case '3': if(cockpit()->dock_mid) cockpit_show_page((int)(k - '1')); break;
        case LV_KEY_ESC: s_quit = true; break;
        default: break;
    }
}

static void add_key_sink(lv_obj_t *panel)
{
    lv_obj_t *sink = lv_obj_create(panel);
    lv_obj_remove_style_all(sink);
    lv_obj_set_size(sink, 0, 0);
    lv_obj_add_event_cb(sink, key_cb, LV_EVENT_KEY, NULL);
    lv_group_add_obj(cb_display_key_group(), sink);
    lv_group_focus_obj(sink);
}

int main(int argc, char **argv)
{
    cb_args_t args;
    if(!parse_args(argc, argv, &args)) {
        usage(argv[0]);
        return 2;
    }

    if(args.disp.w == 0) {   /* layout B is designed at 1080 p; frame D at the 1920x720 panel */
        args.disp.w = 1920;
        args.disp.h = args.layout == CB_LAYOUT_D ? CB_PANEL_H : 1080;
    }
    lv_init();
    cb_theme_init();
    lv_display_t *disp = cb_display_create(&args.disp);
    if(!disp) {
        fprintf(stderr, "cb_lvgl: display init failed (renderer=%s)\n", cb_display_renderer_name());
        return 1;
    }
    printf("cb_lvgl renderer=%s video=%s display=%dx%d\n", cb_display_renderer_name(), cb_display_video_driver(),
           (int)lv_display_get_horizontal_resolution(disp), (int)lv_display_get_vertical_resolution(disp));
    fflush(stdout);
    if(args.perf) lv_sysmon_show_performance(disp);
    else {   /* compiled in for the gate; silent unless asked */
        lv_sysmon_hide_performance(disp);
        lv_sysmon_performance_pause(disp);
    }

    if(args.gate) {
        gate_screen_create(cb_display_panel());
    }
    else {
        cb_app_build(cb_display_panel(), &(cb_app_cfg_t){ .demo = args.demo, .reduce_motion = args.reduce_motion, .layout = args.layout,
                                                              .live_url = args.live_url, .replay = args.replay,
                                                              .replay_speed = args.replay_speed });
        add_key_sink(cb_display_panel());
        if(args.page > 0 && cockpit()->dock_mid) cockpit_show_page(args.page > 2 ? 2 : args.page);
        if(args.map_day) lv_timer_set_repeat_count(lv_timer_create(map_day_cb, 3000, NULL), 1);
        if(args.settings) lv_timer_set_repeat_count(lv_timer_create(settings_cb, 2500, NULL), 1);
    }
    if(args.snapshot) cb_snapshot_after(cb_display_panel(), args.snapshot, args.snapshot_after_ms);

    uint32_t start = lv_tick_get();
    for(;;) {
        uint32_t t = lv_timer_handler();
        if(s_quit || (args.quit_after_s && lv_tick_elaps(start) >= args.quit_after_s * 1000u)) break;
        lv_delay_ms(t < 5 ? 5 : (t > 30 ? 30 : t));
    }
    /* deleting the UI joins the map's render thread before MapLibre's own teardown runs at exit */
    lv_obj_delete(cb_display_panel());
    return 0;
}
