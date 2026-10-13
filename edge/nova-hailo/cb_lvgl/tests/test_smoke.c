#include "cb_test.h"
#include "cb_app.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "nova/nova_bar.h"
#include <string.h>

/* Headless end-to-end run of the real app (sw renderer): a minute of the scripted demo on a 1920x720
   display, then what a person would notice: an LVGL warning, anything escaping its card or the panel, a bar
   that loses its state under rapid tuck/expand, a panel that is not centred on a taller screen, and long
   strings that wrap instead of ending in dots. */

static uint32_t s_ms;
static int s_warnings;

static uint32_t tick_cb(void) { return s_ms; }

static void log_cb(lv_log_level_t level, const char *buf)
{
    if(level != LV_LOG_LEVEL_WARN && level != LV_LOG_LEVEL_ERROR) return;   /* sysmon prints at USER level */
    s_warnings++;
    fputs(buf, stderr);
}

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    LV_UNUSED(a);
    LV_UNUSED(px);
    lv_display_flush_ready(d);
}

static lv_display_t *headless(int32_t w, int32_t h)
{
    static uint8_t bufs[2][CB_PANEL_W * 80 * 4];
    static int used;
    lv_display_t *d = lv_display_create(w, h);
    lv_display_set_flush_cb(d, flush_cb);
    lv_display_set_buffers(d, bufs[used++], NULL, sizeof bufs[0], LV_DISPLAY_RENDER_MODE_PARTIAL);
    return d;
}

static void run_ms(uint32_t ms)
{
    for(uint32_t t = 0; t < ms; t += 10) {
        s_ms += 10;
        lv_timer_handler();
    }
}

static int32_t px_sum(const lv_draw_buf_t *b, int32_t x, int32_t y)
{
    const uint8_t *p = b->data + (size_t)y * b->header.stride + (size_t)x * 4;
    return p[0] + p[1] + p[2];
}

/* ---------- containment ---------- */

static int s_escapes, s_visited;

static bool inside(const lv_area_t *c, const lv_area_t *p)
{
    return c->x1 >= p->x1 - 1 && c->y1 >= p->y1 - 1 && c->x2 <= p->x2 + 1 && c->y2 <= p->y2 + 1;
}

/* Children must stay inside their parent unless the parent opts into overflow (the orb's glow). */
static void check_tree(lv_obj_t *o, const lv_area_t *panel)
{
    lv_area_t pa;
    lv_obj_get_coords(o, &pa);
    bool clips = !lv_obj_is_overflow_visible(o);
    for(uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *c = lv_obj_get_child(o, (int32_t)i);
        if(lv_obj_is_hidden(c)) continue;
        s_visited++;
        lv_area_t ca;
        lv_obj_get_coords(c, &ca);
        if(!inside(&ca, panel) || (clips && !inside(&ca, &pa))) {
            if(s_escapes < 8)
                fprintf(stderr, "escape: child %u of %p (%s) at %d,%d..%d,%d, parent %d,%d..%d,%d\n", (unsigned)i, (void *)o,
                        lv_obj_check_type(c, &lv_label_class) ? lv_label_get_text(c) : "obj", (int)ca.x1, (int)ca.y1,
                        (int)ca.x2, (int)ca.y2, (int)pa.x1, (int)pa.y1, (int)pa.x2, (int)pa.y2);
            s_escapes++;
        }
        if(clips) check_tree(c, panel);
    }
}

static void check_containment(lv_obj_t *panel)
{
    lv_obj_update_layout(panel);
    lv_area_t a;
    lv_obj_get_coords(panel, &a);
    check_tree(panel, &a);
}

/* ---------- long strings ---------- */

static char s_long[301];
static int s_wrapped;

/* Every label that promises dots must stay exactly one line tall whatever it is given. */
static void check_dots(lv_obj_t *o)
{
    for(uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *c = lv_obj_get_child(o, (int32_t)i);
        if(lv_obj_check_type(c, &lv_label_class) && lv_label_get_long_mode(c) == LV_LABEL_LONG_MODE_DOTS) {
            lv_label_set_text(c, s_long);
            lv_obj_update_layout(c);
            int32_t line = lv_font_get_line_height(lv_obj_get_style_text_font(c, 0));
            if(lv_obj_get_height(c) != line) {
                fprintf(stderr, "wrapped: label height %d, line %d\n", (int)lv_obj_get_height(c), (int)line);
                s_wrapped++;
            }
        }
        check_dots(c);
    }
}

/* ---------- worst-case last turn: 3 calls and 3 diffs with long names ---------- */

static const cb_last_turn_t k_max = {
    "please make the whole cabin warmer, open every window a little and call Sam back on speaker", "jev", "118 + 131 ms",
    { { "hvac \xC2\xB7 set_temperature_for_every_zone", "driver +1 \xC2\xB0" "C, passenger +1, rear +1", CB_VERDICT_EXECUTE },
      { "windows \xC2\xB7 set_window", "zone=all, value=10", CB_VERDICT_CONFIRM },
      { "phone \xC2\xB7 call_contact", "Sam (mobile), speaker", CB_VERDICT_DECLINE } }, 3,
    { { "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", "20.0 \xE2\x86\x92 21.0" },
      { "Vehicle.Cabin.Door.Row2.PassengerSide.Window.Position", "0 \xE2\x86\x92 10" },
      { "Nova.Phone.CallState", "idle \xE2\x86\x92 dialing" } }, 3,
};

int main(void)
{
    lv_init();
    lv_tick_set_cb(tick_cb);
    lv_log_register_print_cb(log_cb);
    cb_theme_init();
    memset(s_long, 'W', sizeof s_long - 1);

    lv_display_t *disp = headless(CB_PANEL_W, CB_PANEL_H);
    lv_sysmon_hide_performance(disp);
    lv_sysmon_performance_pause(disp);
    lv_obj_t *panel = cb_panel_create(lv_display_get_screen_active(disp));
    cb_app_build(panel, &(cb_app_cfg_t){ .demo = true });

    /* A minute of the demo, checking geometry every second. */
    for(int s = 0; s < 60; s++) {
        run_ms(1000);
        check_containment(panel);
    }
    CHECK_EQ(s_escapes, 0);
    CHECK(s_visited / 60 > 300);   /* the walk really covers the dashboard */

    /* Worst-case last turn still fits the Connected column. */
    s_escapes = 0;
    cb_model_set_last_turn(&k_max);
    check_containment(panel);
    CHECK_EQ(s_escapes, 0);

    /* Rapid tuck/expand ends open, with the text visible and fully opaque. */
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(50);
    cb_app_event(NV_EV_TAP_ORB, NULL);
    run_ms(50);
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(50);
    cb_app_event(NV_EV_TAP_ORB, NULL);
    run_ms(1000);
    nova_bar_t *bar = cb_app_bar();
    lv_obj_t *text_col = nova_bar_text_col(bar);
    CHECK_EQ(lv_obj_get_width(nova_bar_obj(bar)), 760);
    CHECK(!lv_obj_is_hidden(text_col));
    CHECK_EQ(lv_obj_get_style_opa(text_col, 0), LV_OPA_COVER);

    /* The listening orb's glow reaches past the orb's own square (clipped to it, it showed as a square). */
    lv_obj_t *bar_obj = nova_bar_obj(bar), *orb = nova_bar_orb(bar);
    lv_obj_update_layout(bar_obj);
    lv_draw_buf_t *shot = lv_snapshot_take(bar_obj, LV_COLOR_FORMAT_XRGB8888);
    CHECK(shot != NULL);
    if(shot) {
        lv_area_t ba, oa;
        lv_obj_get_coords(bar_obj, &ba);
        lv_obj_get_coords(orb, &oa);
        int32_t ext = ((int32_t)shot->header.w - lv_area_get_width(&ba)) / 2;   /* snapshot margin */
        int32_t glow = px_sum(shot, oa.x1 - 4 - ba.x1 + ext, (oa.y1 + oa.y2) / 2 - ba.y1 + ext);
        int32_t plain_bg = px_sum(shot, lv_area_get_width(&ba) / 2 + ext, 4 + ext);
        if(glow <= plain_bg + 15) fprintf(stderr, "glow outside the orb square: %d vs background %d\n", (int)glow, (int)plain_bg);
        CHECK(glow > plain_bg + 15);
        lv_draw_buf_destroy(shot);
    }

    /* A 300-char transcript through the real typewriter, then every dots label in the tree. */
    nova_bar_set_text(bar, s_long, false);
    run_ms(5000);
    check_dots(panel);
    CHECK_EQ(s_wrapped, 0);

    CHECK_EQ(s_warnings, 0);

    /* The 1920x720 panel sits centred on a 1920x1080 screen. */
    lv_display_t *tall = headless(1920, 1080);
    lv_obj_t *p2 = cb_panel_create(lv_display_get_screen_active(tall));
    lv_obj_update_layout(p2);
    lv_area_t a;
    lv_obj_get_coords(p2, &a);
    CHECK_EQ(a.x1, 0);
    CHECK_EQ(a.y1, 180);

    DONE();
}
