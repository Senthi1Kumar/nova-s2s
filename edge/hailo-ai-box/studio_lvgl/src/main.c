#include "app.h"
#include "cb_snapshot.h"
#include "display.h"
#include "net.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    cb_display_cfg_t disp;
    const char *url, *key, *snapshot, *tour, *chat, *vtask, *vsrc;
    uint32_t vcycle_s, quit_after_s, snapshot_after_ms, tour_ms;
    int page;
    bool perf, reduce_motion, chat_focus;
} args_t;

static void usage(const char *a)
{
    fprintf(stderr,
            "usage: %s [--fullscreen] [--url http://127.0.0.1:7442] [--key KEY] [--size WxH] [--page 0..6: 0 Overview (default), 1 Voice AI (opens the microphone), 2 Computer Vision, 3 Chat & VLM, 4 Face ID, 5 Device Status, 6 Inspector] [--perf]\n"
            "          [--reduce-motion] [--quit-after SEC] [--snapshot out.ppm [--snapshot-after MS]]\n"
            "          [--chat TEXT] [--chat-focus] [--vtask-cycle SEC] [--vtask detection|pose|segmentation|lanes|dms|ocr] [--vsource c920e|f455] [--tour DIR [--tour-ms MS]]   (snapshot every page into DIR/page_N.ppm, then quit)\n",
            a);
}

static bool parse_args(int argc, char **argv, args_t *a)
{
    *a = (args_t){ .url = "http://127.0.0.1:7442", .snapshot_after_ms = 2500, .tour_ms = 3000, .page = 0 };
    for(int i = 1; i < argc; i++) {
        const char *s = argv[i];
        bool more = i + 1 < argc;
        if(!strcmp(s, "--fullscreen")) a->disp.fullscreen = true;
        else if(!strcmp(s, "--url") && more) a->url = argv[++i];
        else if(!strcmp(s, "--key") && more) a->key = argv[++i];
        else if(!strcmp(s, "--page") && more) a->page = atoi(argv[++i]);
        else if(!strcmp(s, "--perf")) a->perf = true;
        else if(!strcmp(s, "--reduce-motion")) a->reduce_motion = true;
        else if(!strcmp(s, "--quit-after") && more) a->quit_after_s = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if(!strcmp(s, "--snapshot") && more) a->snapshot = argv[++i];
        else if(!strcmp(s, "--snapshot-after") && more) a->snapshot_after_ms = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if(!strcmp(s, "--vtask") && more) a->vtask = argv[++i];
        else if(!strcmp(s, "--vsource") && more) a->vsrc = argv[++i];
        else if(!strcmp(s, "--vtask-cycle") && more) a->vcycle_s = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if(!strcmp(s, "--chat-focus")) a->chat_focus = true;
        else if(!strcmp(s, "--chat") && more) a->chat = argv[++i];
        else if(!strcmp(s, "--tour") && more) a->tour = argv[++i];
        else if(!strcmp(s, "--tour-ms") && more) a->tour_ms = (uint32_t)strtoul(argv[++i], NULL, 10);
        else if(!strcmp(s, "--size") && more) {
            int w, h;
            if(sscanf(argv[++i], "%dx%d", &w, &h) != 2 || w < 640 || h < 480) return false;
            a->disp.w = w;
            a->disp.h = h;
        }
        else return false;
    }
    return true;
}

static struct { const char *dir; int next; char path[256]; } T;

static void tour_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    if(T.next > 0) {
        snprintf(T.path, sizeof T.path, "%s/page_%d.ppm", T.dir, T.next - 1);
        printf("snapshot %s %s\n", T.path, cb_snapshot_ppm(cb_display_root(), T.path) ? "written" : "FAILED");
        fflush(stdout);
    }
    if(T.next >= app_page_count()) {
        app_request_quit();
        return;
    }
    app_goto(T.next++);
}

int main(int argc, char **argv)
{
    args_t a;
    if(!parse_args(argc, argv, &a)) {
        usage(argv[0]);
        return 2;
    }
    if(!net_start(a.url, a.key)) {
        fprintf(stderr, "aibox_studio: bad --url '%s'\n", a.url);
        return 2;
    }
    if(a.disp.w == 0) {   /* fullscreen resizes the window to the desktop; this is only the first size */
        a.disp.w = 1280;
        a.disp.h = 800;
    }
    lv_init();
    cb_theme_init();
    lv_display_t *disp = cb_display_create(&a.disp);
    if(!disp) {
        fprintf(stderr, "aibox_studio: display init failed (renderer=%s)\n", cb_display_renderer_name());
        return 1;
    }
    printf("aibox_studio renderer=%s video=%s display=%dx%d url=%s\n", cb_display_renderer_name(), cb_display_video_driver(),
           (int)lv_display_get_horizontal_resolution(disp), (int)lv_display_get_vertical_resolution(disp), net_base_url());
    fflush(stdout);
    if(a.perf) lv_sysmon_show_performance(disp);
    else {
        lv_sysmon_hide_performance(disp);
        lv_sysmon_performance_pause(disp);
    }

    if(a.chat) chat_demo(a.chat);
    if(a.chat_focus) chat_demo_focus();
    if(a.vtask || a.vsrc) vision_demo(a.vtask, a.vsrc);
    app_build(cb_display_root(), &(app_cfg_t){ .page = a.page, .reduce_motion = a.reduce_motion });
    if(a.vcycle_s) vision_cycle(a.vcycle_s * 1000u);
    if(a.snapshot) cb_snapshot_after(cb_display_root(), a.snapshot, a.snapshot_after_ms);
    if(a.tour) {
        T.dir = a.tour;
        lv_timer_create(tour_cb, a.tour_ms, NULL);
    }

    uint32_t start = lv_tick_get();
    for(;;) {
        uint32_t t = lv_timer_handler();
        if(app_quit_requested() || (a.quit_after_s && lv_tick_elaps(start) >= a.quit_after_s * 1000u)) break;
        lv_delay_ms(t < 3 ? 3 : (t > 20 ? 20 : t));
    }
    net_stop();
    lv_obj_delete(cb_display_root());
    return 0;
}
