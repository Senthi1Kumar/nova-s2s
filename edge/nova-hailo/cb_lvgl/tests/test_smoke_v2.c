#include "cb_test.h"
#include "cb_app.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "nova/avatar.h"
#include "nova/avatar_test.h"
#include "nova/nova_panel.h"
#include "screens/cockpit.h"
#include "live/live.h"
#include <string.h>

/* Headless run of layout v2, the avatar cockpit, at 1920x1080 on the sw renderer: twenty seconds of the demo with
   the Nova panel kept inside its room above the tiles, rapid tuck/expand that settles open, a long question that
   stays one sliding line, a long answer card folded into the panel's budget, the green notification dot while
   tucked, a tile glow that is over within 2.1 s, and not one LVGL warning. */


static lv_obj_t *find_label(lv_obj_t *o, const char *text, bool prefix)
{
    if(lv_obj_check_type(o, &lv_label_class)) {
        const char *t = lv_label_get_text(o);
        if(prefix ? !strncmp(t, text, strlen(text)) : !strcmp(t, text)) return o;
    }
    for(uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
        lv_obj_t *r = find_label(lv_obj_get_child(o, (int32_t)i), text, prefix);
        if(r) return r;
    }
    return NULL;
}

static bool has_chart(lv_obj_t *o)
{
    if(lv_obj_check_type(o, &lv_chart_class)) return true;
    for(uint32_t i = 0; i < lv_obj_get_child_count(o); i++)
        if(has_chart(lv_obj_get_child(o, (int32_t)i))) return true;
    return false;
}

/* The speed card is the second ancestor of its "km/h" label. */
static lv_obj_t *speed_card(lv_obj_t *root)
{
    lv_obj_t *k = find_label(root, "km/h", false);
    return k ? lv_obj_get_parent(lv_obj_get_parent(k)) : NULL;
}

static uint32_t s_ms;
static int s_warnings;

static uint32_t tick_cb(void) { return s_ms; }

static void log_cb(lv_log_level_t level, const char *buf)
{
    if(level != LV_LOG_LEVEL_WARN && level != LV_LOG_LEVEL_ERROR) return;
    s_warnings++;
    fputs(buf, stderr);
}

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    LV_UNUSED(a);
    LV_UNUSED(px);
    lv_display_flush_ready(d);
}

static void run_ms(uint32_t ms)
{
    for(uint32_t t = 0; t < ms; t += 10) {
        s_ms += 10;
        lv_timer_handler();
    }
}

static const nova_card_view_t k_card = {
    .kind = "results", .title = "Fast chargers near Palo Alto",
    .rows = { { "Tesla Supercharger \xC2\xB7 Stanford Shopping Center", "1.2 km" }, { "Electrify America", "2.8 km" },
              { "EVgo \xC2\xB7 Town & Country Village", "3.1 km" }, { "ChargePoint \xC2\xB7 Cal Ave garage", "3.9 km" } },
    .n_rows = 4, .more = 2,
};

/* How far a colour is from grey: max - min of its channels, in percent. */
static float chroma_pct(lv_color_t c)
{
    int hi = c.red > c.green ? (c.red > c.blue ? c.red : c.blue) : (c.green > c.blue ? c.green : c.blue);
    int lo = c.red < c.green ? (c.red < c.blue ? c.red : c.blue) : (c.green < c.blue ? c.green : c.blue);
    return 100.0f * (float)(hi - lo) / 255.0f;
}

int main(void)
{
    lv_init();
    lv_tick_set_cb(tick_cb);
    lv_log_register_print_cb(log_cb);
    cb_theme_init();

    static uint8_t buf[1920 * 80 * 4];
    lv_display_t *disp = lv_display_create(1920, 1080);
    lv_display_set_flush_cb(disp, flush_cb);
    lv_display_set_buffers(disp, buf, NULL, sizeof buf, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_sysmon_hide_performance(disp);
    lv_sysmon_performance_pause(disp);
    lv_obj_t *panel = cb_panel_create(lv_display_get_screen_active(disp));
    cb_app_build(panel, &(cb_app_cfg_t){ .demo = true, .layout = CB_LAYOUT_V2 });

    nova_panel_t *np = cb_app_panel();
    CHECK(np != NULL);
    CHECK(cb_app_bar() == NULL);
    if(!np) return 1;
    lv_obj_t *box = nova_panel_obj(np);
    int32_t budget = cockpit()->nova_max_h;
    CHECK(budget > 200);

    /* The demo runs Nova through every state: the panel never grows into the tiles. */
    for(int s = 0; s < 20; s++) {
        run_ms(1000);
        CHECK(lv_obj_get_height(box) <= budget);
    }
    cb_app_toggle_demo_pause();
    run_ms(1500);

    /* Rapid tuck/expand settles open and fully opaque. */
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(1000);
    int32_t rest_h = lv_obj_get_height(box);
    cb_app_event(NV_EV_TAP_ORB, NULL);
    run_ms(50);
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(50);
    cb_app_event(NV_EV_TAP_ORB, NULL);
    run_ms(1200);
    CHECK(!nova_panel_is_animating(np));
    CHECK(cb_app_fsm()->view == NV_VIEW_OPEN);

    /* A long question is one line that slides, never a wrap. */
    lv_obj_t *line = nova_panel_line(np);
    nova_panel_set_text(np, "could you please find me a quiet coffee place with parking near the next charging stop "
                            "on the way to San Francisco and tell me how long the detour takes", true);
    lv_obj_update_layout(line);
    CHECK_EQ(lv_label_get_long_mode(line), LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    CHECK_EQ(lv_obj_get_height(line), lv_font_get_line_height(lv_obj_get_style_text_font(line, 0)));

    /* A long reply and a six-item card: the open panel stays within its budget, rows fold into "+N more". */
    nova_panel_stream_text(np, "There are four fast chargers within four kilometres. The Supercharger at the Stanford "
                               "Shopping Center is closest and has eight free stalls right now, the others are busier.");
    nova_panel_set_card(np, &k_card);
    run_ms(1200);
    CHECK(!nova_panel_is_animating(np));
    CHECK(lv_obj_get_height(box) > rest_h);
    CHECK(lv_obj_get_height(box) <= budget);

    /* Tucked, a new card is a green dot; opening reads it. */
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(1200);
    CHECK_EQ(lv_obj_get_height(box), rest_h);
    nova_panel_set_card(np, &k_card);
    CHECK(nova_panel_notified(np));
    run_ms(200);
    CHECK_EQ(lv_obj_get_height(box), rest_h);   /* tucked stays tucked */
    cb_app_event(NV_EV_TAP_ORB, NULL);
    run_ms(1200);
    CHECK(!nova_panel_notified(np));

    /* A tile's gradient glow is over within 2.1 s. */
    nova_panel_set_text(np, "short", true);   /* no marquee animation in the count */
    run_ms(200);
    uint32_t base = lv_anim_count_running();
    cb_model_set_int(cb_model()->mirrors_folded, !lv_subject_get_int(cb_model()->mirrors_folded));
    run_ms(100);
    CHECK(lv_anim_count_running() > base);
    run_ms(2000);
    CHECK_EQ(lv_anim_count_running(), base);

    /* A lone avatar: the 33 ms timer stops once nothing moves, any setter restarts it, and the next blink wakes it. */
    lv_obj_t *av = avatar_create(panel, 150);
    run_ms(2000);
    CHECK(!avatar_timer_running(av));
    avatar_set_state(av, NV_LISTENING, false);
    CHECK(avatar_timer_running(av));
    avatar_set_state(av, NV_IDLE, false);
    run_ms(2000);
    CHECK(!avatar_timer_running(av));
    bool woke = false;
    for(int i = 0; i < 700 && !woke; i++) {   /* the first blink is 3..6 s out; stop there */
        run_ms(10);
        woke = avatar_timer_running(av);
    }
    CHECK(woke);
    run_ms(500);
    CHECK(!avatar_timer_running(av));

    /* Every head builds with at most eight parts, in every state. */
    for(int h = 0; h < AV_HEAD_COUNT; h++) {
        avatar_set_head(av, (av_head_t)h);
        for(int st = 0; st < AV_STATE_COUNT; st++) {
            avatar_set_state(av, st == AV_ST_LISTENING ? NV_LISTENING : st == AV_ST_THINKING ? NV_THINKING :
                                 st == AV_ST_SPEAKING ? NV_SPEAKING : NV_IDLE, st == AV_ST_MUTED);
            if(st == AV_ST_GREETING) avatar_greet(av);
            run_ms(120);
            CHECK(lv_obj_get_child_count(av) <= 8);
        }
    }
    avatar_set_head(av, AV_HEAD_PEBBLE);

    /* Muted is grey: body and eyes. */
    avatar_set_state(av, NV_IDLE, true);
    run_ms(1500);
    CHECK(chroma_pct(avatar_body_color(av)) <= 5.0f);
    CHECK(chroma_pct(avatar_eye_color(av)) <= 5.0f);
    avatar_set_state(av, NV_LISTENING, false);
    run_ms(1500);
    CHECK(chroma_pct(avatar_body_color(av)) > 20.0f);

    /* Under reduce-motion it settles at once and never wakes for a blink. */
    cb_theme_set_reduce_motion(true);
    avatar_set_state(av, NV_SPEAKING, false);
    run_ms(300);
    CHECK(!avatar_timer_running(av));
    woke = false;
    for(int i = 0; i < 700; i++) {
        run_ms(10);
        woke = woke || avatar_timer_running(av);
    }
    CHECK(!woke);
    cb_theme_set_reduce_motion(false);
    lv_obj_delete(av);
    /* A still face needs no transform layer: idle, neutral mood, nothing rotated or scaled. */
    lv_obj_t *calm = avatar_create(panel, 150);
    run_ms(1500);
    for(uint32_t i = 0; i < lv_obj_get_child_count(calm); i++) {
        lv_obj_t *c = lv_obj_get_child(calm, (int32_t)i);
        if(lv_obj_is_hidden(c)) continue;
        CHECK_EQ(lv_obj_get_style_transform_rotation(c, 0), 0);
        CHECK_EQ(lv_obj_get_style_transform_scale_x(c, 0), 256);
        CHECK_EQ(lv_obj_get_style_transform_scale_y(c, 0), 256);
    }
    lv_obj_delete(calm);

    /* An avatar made after a mood change starts with that mood. */
    cb_app_nova_mood_event(NM_TOOL_ERROR);
    run_ms(200);
    lv_obj_t *late = avatar_create(panel, 150);
    CHECK_EQ(avatar_model(late)->mood, AV_CONCERNED);
    lv_obj_delete(late);


    /* topbar_has_no_model_chip */
    lv_obj_t *strip = lv_obj_get_parent(cockpit()->link_chip);
    CHECK(find_label(strip, "Model", true) == NULL);

    /* speed_card_has_no_sparkline */
    lv_obj_t *sc = speed_card(lv_screen_active());
    CHECK(sc != NULL);
    if(sc) CHECK(!has_chart(sc));

    /* nova_page_has_settings */
    cockpit_show_page(2);
    run_ms(1000);
    lv_obj_update_layout(panel);
    const char *secs[3] = { "LANGUAGE MODEL", "VOICE", "AVATAR" };
    for(int i = 0; i < 3; i++) {
        lv_obj_t *l = find_label(lv_screen_active(), secs[i], false);
        CHECK(l != NULL);
        if(l) {
            lv_area_t a;
            lv_obj_get_coords(l, &a);
            CHECK(a.x1 >= 0 && a.x2 <= 1920);
        }
    }

    CHECK_EQ(s_warnings, 0);
    lv_obj_delete(panel);   /* joins the map's worker before exit, as main does */
    DONE();
}
