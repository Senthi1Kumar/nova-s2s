#include "cb_test.h"
#include "cb_app.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "nova/avatar_test.h"
#include "nova/nova_panel.h"
#include "screens/cockpit.h"
#include "live/live.h"
#include "widgets/settings_sheet.h"
#include <string.h>
#include <strings.h>

/* Headless run of layout B, the Dock Charm, at 1920x1080 on the sw renderer: the avatar (no orb, no dot) sits on
   the glass bar, an answer card springs the bar upward with its bottom fixed, the bar tucks 8 s after the answer
   unless a card holds it, long text stays inside it, and the glass takes the speaker's tint. */


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

static void click_label(const char *text)
{
    lv_obj_t *l = find_label(lv_screen_active(), text, false);
    CHECK(l != NULL);
    if(l) lv_obj_send_event(lv_obj_get_parent(l), LV_EVENT_CLICKED, NULL);
}

static bool label_checked(const char *text)
{
    lv_obj_t *l = find_label(lv_screen_active(), text, false);
    return l && lv_obj_has_state(lv_obj_get_parent(l), LV_STATE_CHECKED);
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
    .rows = { { "Tesla Supercharger", "1.2 km" }, { "Electrify America", "2.8 km" }, { "EVgo", "3.1 km" } },
    .n_rows = 3, .more = 1,
};

/* One answered turn: wake, think, speak, done. */
static void answer_ex(bool with_card, bool expects)
{
    nv_done_t done = { .expects_answer = expects, .outcome = NV_OUT_DONE };
    cb_app_event(NV_EV_WAKE_TAP, NULL);
    cb_app_event(NV_EV_TURN_COMMITTED, NULL);
    cb_app_event(NV_EV_AGENT_THINKING, NULL);
    cb_app_event(NV_EV_AGENT_SPEAKING, NULL);
    cb_app_nova_stream("Here is what I found.");
    if(with_card) cb_app_nova_card(&k_card);
    run_ms(500);
    cb_app_event(NV_EV_AGENT_DONE, &done);
}

static void answer(bool with_card) { answer_ex(with_card, false); }

static void tuck_now(nova_panel_t *np)
{
    cb_app_event(NV_EV_TAP_TUCK, NULL);
    run_ms(1500);
    CHECK(!nova_panel_expanded(np));
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
    cb_app_build(panel, &(cb_app_cfg_t){ .demo = false, .layout = CB_LAYOUT_B });

    nova_panel_t *np = cb_app_panel();
    CHECK(np != NULL);
    if(!np) return 1;
    lv_obj_t *box = nova_panel_obj(np);
    run_ms(1000);

    /* dock_has_avatar_not_orb */
    CHECK(cb_app_bar() == NULL);
    CHECK(nova_panel_avatar(np) != NULL);
    CHECK(!nova_panel_expanded(np));
    lv_area_t tucked;
    lv_obj_update_layout(panel);
    lv_obj_get_coords(box, &tucked);

    /* The avatar sits partly above the bar. */
    lv_area_t aa;
    lv_obj_get_coords(nova_panel_avatar(np), &aa);
    CHECK(aa.y1 < tucked.y1 - 20);

    /* card_expands_upward */
    cb_app_nova_card(&k_card);
    run_ms(1500);
    lv_area_t open;
    lv_obj_update_layout(panel);
    lv_obj_get_coords(box, &open);
    CHECK(nova_panel_expanded(np));
    CHECK_EQ(open.y2, tucked.y2);
    CHECK(open.y1 < tucked.y1);
    CHECK(!nova_panel_notified(np));

    /* The open bar stays over the map: clear of the left column, short of the media widget. */
    lv_area_t car;
    lv_obj_get_coords(cockpit()->car_col, &car);
    CHECK(open.x1 >= car.x2);
    CHECK(open.x1 >= cockpit()->nova_x1 && open.x2 <= cockpit()->nova_x2);
    CHECK(tucked.x1 >= cockpit()->nova_x1 && tucked.x2 <= cockpit()->nova_x2);
    CHECK_EQ(lv_obj_get_style_bg_opa(box, 0), 200);
#if CB_RENDER_GPU
    CHECK(lv_obj_get_style_blur_backdrop(box, 0));   /* the GPU build blurs the dock by default */
    CHECK_EQ(lv_obj_get_style_blur_radius(box, 0), 22);
#else
    CHECK(!lv_obj_get_style_blur_backdrop(box, 0));
    CHECK_EQ(lv_obj_get_style_blur_radius(box, 0), 0);
#endif

    /* buttons: 52 px circles, the end one solid red */
    for(int i = 0; i < 3; i++) {
        lv_obj_t *b = nova_panel_button(np, (nova_btn_t)i);
        CHECK_EQ(lv_obj_get_width(b), 52);
        CHECK_EQ(lv_obj_get_height(b), 52);
    }
    lv_obj_t *endb = nova_panel_button(np, NOVA_BTN_END);
    lv_color_t rc = lv_obj_get_style_bg_color(endb, 0);
    CHECK(rc.red == cb_col(CB_RED).red && rc.green == cb_col(CB_RED).green && rc.blue == cb_col(CB_RED).blue);
    CHECK_EQ(lv_obj_get_style_bg_opa(endb, 0), 255);

    /* the card: no "results" kind label, "+N more" a small footer inside it */
    lv_obj_t *card = nova_panel_card(np);
    lv_area_t ca;
    lv_obj_get_coords(card, &ca);
    uint32_t nlbl = 0, nmore = 0;
    for(lv_obj_t *o = card; o; o = NULL) {
        for(uint32_t i = 0; i < lv_obj_get_child_count(o); i++) {
            lv_obj_t *ch = lv_obj_get_child(o, (int32_t)i);
            lv_obj_t *stack[16] = { ch };
            int sp = 1;
            while(sp) {
                lv_obj_t *x = stack[--sp];
                if(lv_obj_check_type(x, &lv_label_class)) {
                    const char *tx = lv_label_get_text(x);
                    nlbl++;
                    CHECK(strcasecmp(tx, "results") != 0);
                    if(strstr(tx, "more")) {
                        lv_area_t ma2;
                        lv_obj_get_coords(x, &ma2);
                        nmore++;
                        CHECK(ma2.x1 >= ca.x1 && ma2.x2 <= ca.x2 && ma2.y1 >= ca.y1 && ma2.y2 <= ca.y2);
                        CHECK(!lv_obj_is_hidden(x));
                    }
                }
                for(uint32_t k = 0; k < lv_obj_get_child_count(x) && sp < 15; k++) stack[sp++] = lv_obj_get_child(x, (int32_t)k);
            }
        }
    }
    CHECK(nlbl > 3);
    CHECK_EQ(nmore, 1);

    /* tucks_after_done: a card holds the bar open, no card lets it tuck */
    answer(true);
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(nova_panel_expanded(np));
    tuck_now(np);
    answer(false);
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(!nova_panel_expanded(np));
    CHECK(!nova_panel_notified(np));

    /* A turn that goes on keeps the bar open past the tuck timeout, however it goes on. */
    tuck_now(np);
    answer_ex(false, true);   /* (a) the driver keeps waking Nova: LISTENING is re-entered through the FSM */
    for(int i = 0; i < 4; i++) {   /* 12 s: past the 8 s tuck timeout, open at every step */
        run_ms(3000);
        CHECK(cb_app_fsm()->activity != NV_IDLE);
        CHECK(nova_panel_expanded(np));
        cb_app_event(NV_EV_WAKE_TAP, NULL);
    }
    tuck_now(np);
    answer_ex(false, true);   /* (b) the driver is speaking: the window cannot close */
    cb_app_event(NV_EV_USER_SPEAKING_ON, NULL);
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(cb_app_fsm()->user_speaking);
    CHECK(cb_app_fsm()->activity != NV_IDLE);
    CHECK(nova_panel_expanded(np));
    /* The turn ends: idle, then it tucks within the timeout. */
    cb_app_event(NV_EV_USER_SPEAKING_OFF, NULL);
    cb_app_event(NV_EV_TAP_END, NULL);   /* ends the turn (and tucks); reopen to time the auto-tuck alone */
    run_ms(1500);
    answer(false);
    run_ms(NOVA_PANEL_TUCK_MS + 500);
    CHECK_EQ(cb_app_fsm()->activity, NV_IDLE);
    CHECK(!nova_panel_expanded(np));
    CHECK_EQ(cb_app_fsm()->view, NV_VIEW_REST);

    /* A card arriving while tucked opens the bar, with no dot. */
    cb_app_nova_card(&k_card);
    run_ms(1500);
    CHECK(nova_panel_expanded(np));
    CHECK(!nova_panel_notified(np));
    tuck_now(np);

    /* long_text_stays_inside */
    char user[501], reply[2001];
    memset(user, 'W', 500);
    user[500] = 0;
    memset(reply, 'a', 2000);
    for(int i = 9; i < 2000; i += 10) reply[i] = ' ';
    reply[2000] = 0;
    cb_app_event(NV_EV_WAKE_TAP, NULL);
    cb_app_nova_text(user, true);
    int32_t x_start = lv_obj_get_x(lv_obj_get_child(nova_panel_line(np), 0));
    cb_app_nova_stream(reply);
    run_ms(3000);
    lv_obj_update_layout(panel);
    lv_area_t ba, la;
    lv_obj_get_coords(box, &ba);
    lv_obj_t *line = nova_panel_line(np);
    lv_obj_get_coords(line, &la);
    CHECK(la.x1 >= ba.x1 && la.x2 <= ba.x2 && la.y1 >= ba.y1 && la.y2 <= ba.y2);
    lv_obj_t *lbl = lv_obj_get_child(line, 0);
    CHECK_EQ(x_start, 0);   /* held at first, then sliding */
    run_ms(2500);
    CHECK(lv_obj_get_x(lbl) < 0);
    run_ms(60000);
    CHECK(lv_obj_get_x(lbl) <= 0 && lv_obj_get_x(lbl) >= -lv_obj_get_width(lbl));
    lv_obj_t *bubble = nova_panel_bubble(np);
    CHECK(bubble != NULL);
    if(bubble) {
        lv_area_t bb;
        lv_obj_get_coords(bubble, &bb);
        CHECK(!lv_obj_is_hidden(bubble));
        CHECK(bb.x1 >= ba.x1 && bb.x2 <= ba.x2);
        CHECK((bb.x2 - bb.x1) <= (ba.x2 - ba.x1) * 88 / 100 + 2);
        CHECK(lv_obj_get_height(bubble) <= 2 * 24 + 40);
        lv_area_t ra;
        lv_obj_get_coords(nova_panel_reply(np), &ra);
        CHECK(ra.y1 - bb.y2 <= 24);   /* the reply sits right under the bubble */
    }
    lv_obj_t *reply_lbl = nova_panel_reply(np);
    CHECK(reply_lbl != NULL);
    if(reply_lbl) CHECK(lv_obj_get_height(reply_lbl) <= 4 * (lv_font_get_line_height(lv_obj_get_style_text_font(reply_lbl, 0)) + lv_obj_get_style_text_line_space(reply_lbl, 0)));
    lv_area_t ma;
    lv_obj_get_coords(cockpit()->map, &ma);
    CHECK(ba.y1 >= ma.y1);
    tuck_now(np);

    /* speaker_tint */
    cb_app_event(NV_EV_WAKE_TAP, NULL);
    cb_app_nova_text("turn the heat up", true);
    run_ms(600);
    lv_color_t c = lv_obj_get_style_border_color(box, 0);
    CHECK(c.blue > c.green);
    cb_app_nova_text("Done, it is 22 degrees.", false);
    run_ms(600);
    c = lv_obj_get_style_border_color(box, 0);
    CHECK(c.green > c.blue);

    /* route_line: while a route runs the tucked bar shows the instruction; a spoken prompt only updates it */
    cb_app_event(NV_EV_TAP_END, NULL);
    run_ms(1500);
    CHECK(!nova_panel_expanded(np));
    const char *instr = "In 400 m \xC2\xB7 Bear right onto Emerson Street";
    lv_obj_t *rl = lv_obj_get_child(nova_panel_line(np), 0);
    lv_obj_t *rstate = lv_obj_get_child(lv_obj_get_parent(nova_panel_line(np)), 0);
    cb_app_nova_route_line(instr);
    run_ms(300);
    CHECK(strcmp(lv_label_get_text(rl), instr) == 0);
    CHECK(strstr(lv_label_get_text(rstate), "NAVIGATING") != NULL);
    CHECK(!nova_panel_expanded(np));

    lv_color_t edge = lv_obj_get_style_border_color(box, 0);
    cb_app_nova_guidance("Continue for 1 kilometer.");
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(!nova_panel_expanded(np));
    CHECK(strcmp(lv_label_get_text(rl), "Continue for 1 kilometer.") == 0);
    CHECK(lv_obj_is_hidden(nova_panel_reply(np)) && lv_obj_is_hidden(lv_obj_get_parent(nova_panel_bubble(np))));
    c = lv_obj_get_style_border_color(box, 0);
    CHECK(c.red == edge.red && c.green == edge.green && c.blue == edge.blue);
    cb_app_nova_route_line(instr);   /* the next banner update */
    run_ms(300);
    CHECK(strcmp(lv_label_get_text(rl), instr) == 0);

    /* a real turn during the route behaves as usual; once it tucks, the line is the instruction again */
    cb_app_event(NV_EV_WAKE_TAP, NULL);
    cb_app_nova_text("is there a cafe on the way", true);
    cb_app_event(NV_EV_TURN_COMMITTED, NULL);
    cb_app_event(NV_EV_AGENT_THINKING, NULL);
    cb_app_event(NV_EV_AGENT_SPEAKING, NULL);
    cb_app_nova_stream("Yes, Hilltop Cafe is two minutes ahead.");
    run_ms(500);
    CHECK(nova_panel_expanded(np));
    CHECK(strstr(lv_label_get_text(rl), "cafe on the way") != NULL);
    cb_app_event(NV_EV_AGENT_DONE, &(nv_done_t){ .expects_answer = false, .outcome = NV_OUT_DONE });
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(!nova_panel_expanded(np));
    CHECK(strcmp(lv_label_get_text(rl), instr) == 0);

    /* the route ends: the idle hint returns, and a late prompt does not bring the line back */
    cb_app_nova_route_line(NULL);
    run_ms(300);
    CHECK(strstr(lv_label_get_text(rl), "Hey Nova") != NULL);
    CHECK(strstr(lv_label_get_text(rstate), "NAVIGATING") == NULL);
    cb_app_nova_guidance("Arrive at your destination.");
    run_ms(300);
    CHECK(strstr(lv_label_get_text(rl), "Hey Nova") != NULL);

    /* the route ends during a turn: the quote stays while it is open, the idle hint returns once it tucks */
    cb_app_nova_route_line(instr);
    run_ms(300);
    cb_app_event(NV_EV_WAKE_TAP, NULL);
    cb_app_nova_text("how far is it", true);
    cb_app_event(NV_EV_TURN_COMMITTED, NULL);
    cb_app_event(NV_EV_AGENT_THINKING, NULL);
    cb_app_event(NV_EV_AGENT_SPEAKING, NULL);
    cb_app_nova_stream("About four minutes.");
    run_ms(500);
    cb_app_nova_route_line(NULL);
    run_ms(300);
    CHECK(nova_panel_expanded(np));
    CHECK(strstr(lv_label_get_text(rl), "how far is it") != NULL);
    cb_app_event(NV_EV_AGENT_DONE, &(nv_done_t){ .expects_answer = false, .outcome = NV_OUT_DONE });
    run_ms(NOVA_PANEL_TUCK_MS + 1500);
    CHECK(!nova_panel_expanded(np));
    CHECK(strstr(lv_label_get_text(rl), "Hey Nova") != NULL);


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

    /* offline_settings_disabled: no live link, so every control is off and the page says why */
    CHECK(!live_linked());
    lv_obj_t *ap = find_label(lv_screen_active(), "Apply", false);
    CHECK(ap != NULL);
    if(ap) CHECK(lv_obj_has_state(lv_obj_get_parent(ap), LV_STATE_DISABLED));
    CHECK(find_label(lv_screen_active(), "Connect to Nova to change settings", false) != NULL);

    /* tts_frame_updates_pills */
    const char *fr = "{\"type\":\"nova.settings\",\"tts\":{\"voice\":\"v2\",\"speed\":\"fast\",\"voices\":["
                     "{\"id\":\"v1\",\"label\":\"Amy\"},{\"id\":\"v2\",\"label\":\"Alan\"},{\"id\":\"v3\",\"label\":\"Ryan\"}]},"
                     "\"avatar\":\"gem\"}";
    live_feed(fr, strlen(fr));
    run_ms(100);
    const char *vl[3] = { "Amy", "Alan", "Ryan" };
    for(int i = 0; i < 3; i++) CHECK(find_label(lv_screen_active(), vl[i], false) != NULL);
    lv_obj_t *gem = find_label(lv_screen_active(), "Gem", false);
    CHECK(gem != NULL);
    if(gem) CHECK(lv_obj_has_state(lv_obj_get_parent(gem), LV_STATE_CHECKED));
    lv_obj_t *cap = find_label(lv_screen_active(), "Capsule", false);
    if(cap) CHECK(!lv_obj_has_state(lv_obj_get_parent(cap), LV_STATE_CHECKED));
    lv_obj_t *alan = find_label(lv_screen_active(), "Alan", false);
    if(alan) CHECK(lv_obj_has_state(lv_obj_get_parent(alan), LV_STATE_CHECKED));
    lv_obj_t *fast = find_label(lv_screen_active(), "Fast", false);
    if(fast) CHECK(lv_obj_has_state(lv_obj_get_parent(fast), LV_STATE_CHECKED));


    /* settings_jobs: a link without a pipeline; jobs wait for a fake result */
    settings_sheet_test_mode(true);
    run_ms(600);   /* the settings are unknown: a fetch starts */
    CHECK_EQ(settings_sheet_test_jobs(), 1);
    CHECK(settings_sheet_test_busy() && settings_sheet_test_kind() == 0);
    lv_obj_t *apply = lv_obj_get_parent(find_label(lv_screen_active(), "Apply", false));
    CHECK(lv_obj_has_state(apply, LV_STATE_DISABLED));   /* until the model settings are known */
    settings_sheet_test_finish(false, "boom", NULL, NULL);
    run_ms(2000);
    CHECK_EQ(settings_sheet_test_jobs(), 1);   /* waits */
    run_ms(4000);
    CHECK_EQ(settings_sheet_test_jobs(), 2);   /* retried after about 5 s */
    live_llm_t llm;
    memset(&llm, 0, sizeof llm);
    snprintf(llm.provider, sizeof llm.provider, "local");
    live_tts_t base;
    memset(&base, 0, sizeof base);
    snprintf(base.voice, sizeof base.voice, "v2");
    snprintf(base.speed, sizeof base.speed, "normal");
    snprintf(base.avatar, sizeof base.avatar, "gem");
    base.n = 3;
    const char *ids[3] = { "v1", "v2", "v3" };
    for(int i = 0; i < 3; i++) {
        snprintf(base.v[i].id, sizeof base.v[i].id, "%s", ids[i]);
        snprintf(base.v[i].label, sizeof base.v[i].label, "%s", vl[i]);
    }
    settings_sheet_test_finish(true, NULL, &llm, &base);
    run_ms(2000);
    CHECK_EQ(settings_sheet_test_jobs(), 2);   /* known now: no more fetches */
    apply = lv_obj_get_parent(find_label(lv_screen_active(), "Apply", false));
    CHECK(!lv_obj_has_state(apply, LV_STATE_DISABLED));

    /* a voice, then a speed before the voice finished: the speed is queued and sent after it */
    char sv[48], ss[8], sa[12];
    click_label("Amy");
    CHECK(settings_sheet_test_busy() && settings_sheet_test_kind() == 2);
    settings_sheet_test_sent(sv, ss, sa);
    CHECK(!strcmp(sv, "v1") && !ss[0] && !sa[0]);
    click_label("Slow");
    CHECK_EQ(settings_sheet_test_jobs(), 3);   /* not started yet */
    CHECK(find_label(lv_screen_active(), "One moment", true) != NULL);
    CHECK(label_checked("Slow"));
    live_tts_t now = base;
    snprintf(now.voice, sizeof now.voice, "v1");
    settings_sheet_test_finish(true, NULL, NULL, &now);
    CHECK_EQ(settings_sheet_test_jobs(), 4);
    CHECK(settings_sheet_test_busy());
    settings_sheet_test_sent(sv, ss, sa);
    CHECK(!sv[0] && !strcmp(ss, "slow") && !sa[0]);
    CHECK(label_checked("Amy") && label_checked("Slow"));
    CHECK(find_label(lv_screen_active(), "One moment", true) == NULL);

    /* the speed is refused after a newer frame moved the voice and avatar: only the speed goes back */
    const char *nf = "{\"type\":\"nova.settings\",\"tts\":{\"voice\":\"v3\",\"speed\":\"normal\",\"voices\":["
                     "{\"id\":\"v1\",\"label\":\"Amy\"},{\"id\":\"v2\",\"label\":\"Alan\"},{\"id\":\"v3\",\"label\":\"Ryan\"}]},"
                     "\"avatar\":\"drop\"}";
    live_feed(nf, strlen(nf));
    settings_sheet_test_finish(false, "unknown_speed", NULL, NULL);
    CHECK(!settings_sheet_test_busy());
    CHECK(label_checked("Ryan") && !label_checked("Amy"));
    CHECK(label_checked("Drop"));
    CHECK(label_checked("Normal") && !label_checked("Slow"));
    CHECK(find_label(lv_screen_active(), "Couldn't change it: Unknown speed", true) != NULL);

    /* a speed tap whose job is refused, with nothing newer, goes back to what the pipeline reported */
    click_label("Faster");
    CHECK(label_checked("Faster"));
    settings_sheet_test_finish(false, "starting", NULL, NULL);
    CHECK(label_checked("Normal") && !label_checked("Faster"));
    CHECK(find_label(lv_screen_active(), "Couldn't change it: Nova is starting", true) != NULL);

    /* a preview while busy says so */
    click_label("Fast");
    click_label("Preview");
    CHECK(find_label(lv_screen_active(), "One moment", true) != NULL);
    settings_sheet_test_finish(true, NULL, NULL, &now);

    /* an older pipeline answers without voice settings: an unapplied model pick survives and nothing is refetched */
    settings_sheet_test_forget(true, true);
    run_ms(600);
    int jobs = settings_sheet_test_jobs();
    CHECK(settings_sheet_test_busy() && settings_sheet_test_kind() == 0);
    live_llm_t cl;
    memset(&cl, 0, sizeof cl);
    snprintf(cl.provider, sizeof cl.provider, "local");
    cl.n = 1;
    snprintf(cl.p[0].id, sizeof cl.p[0].id, "groq");
    cl.p[0].ok = true;
    cl.p[0].n_models = 1;
    snprintf(cl.p[0].models[0], sizeof cl.p[0].models[0], "m1");
    settings_sheet_test_finish(false, "this Nova pipeline is too old for that", &cl, NULL);
    CHECK(find_label(lv_screen_active(), "Voice choice needs Piper", false) != NULL);
    CHECK(find_label(lv_screen_active(), "Preview", false) == NULL);   /* nothing to preview, no speeds to set */
    CHECK(find_label(lv_screen_active(), "Fast", false) == NULL);
    click_label("Cloud");   /* the mode pill: picks Groq, not applied */
    CHECK(label_checked("Groq"));
    run_ms(40000);
    CHECK_EQ(settings_sheet_test_jobs(), jobs);
    CHECK(label_checked("Groq"));

    /* a transport failure backs off: 5 s, then 10 s */
    settings_sheet_test_forget(false, true);
    run_ms(600);
    CHECK_EQ(settings_sheet_test_jobs(), jobs + 1);
    settings_sheet_test_finish(false, "timeout", &cl, NULL);
    run_ms(4000);
    CHECK_EQ(settings_sheet_test_jobs(), jobs + 1);
    run_ms(1500);
    CHECK_EQ(settings_sheet_test_jobs(), jobs + 2);
    settings_sheet_test_finish(false, "timeout", &cl, NULL);
    run_ms(8000);
    CHECK_EQ(settings_sheet_test_jobs(), jobs + 2);
    run_ms(3000);
    CHECK_EQ(settings_sheet_test_jobs(), jobs + 3);
    settings_sheet_test_finish(true, NULL, &cl, &now);
    CHECK(label_checked("Groq"));   /* still the unapplied pick */

    /* a fetched avatar choice reaches the dock's head, not only the sheet */
    {
        lv_obj_t *av = nova_panel_avatar(np);
        live_tts_t fetched = now;
        snprintf(fetched.avatar, sizeof fetched.avatar, "%s", avatar_head(av) == AV_HEAD_GEM ? "capsule" : "gem");
        av_head_t want = av_head_parse(fetched.avatar);
        settings_sheet_test_forget(false, true);
        run_ms(600);
        CHECK(settings_sheet_test_busy() && settings_sheet_test_kind() == 0);
        settings_sheet_test_finish(true, NULL, &cl, &fetched);
        CHECK(avatar_head(av) == want);
    }

    /* a duck frame turns the face down with the voice */
    {
        lv_obj_t *av = nova_panel_avatar(np);
        const char *dk = "{\"type\":\"output_audio.duck\",\"gain\":0.3}";
        live_feed(dk, strlen(dk));
        CHECK(avatar_model(av)->duck > 0.29f && avatar_model(av)->duck < 0.31f);
        dk = "{\"type\":\"output_audio.duck\",\"gain\":1.0}";
        live_feed(dk, strlen(dk));
        CHECK(avatar_model(av)->duck > 0.99f);
    }

    settings_sheet_test_mode(false);
    run_ms(600);

    /* a spoken route prompt holds the line about 4 s before banner updates replace it */
    nova_panel_set_route_line(np, "In 300 m \xC2\xB7 Turn left onto Main St");
    run_ms(100);
    CHECK(find_label(lv_screen_active(), "In 300 m \xC2\xB7 Turn left onto Main St", false) != NULL);
    nova_panel_guidance(np, "Keep right at the fork");
    run_ms(100);
    CHECK(find_label(lv_screen_active(), "Keep right at the fork", false) != NULL);
    nova_panel_set_route_line(np, "In 200 m \xC2\xB7 Turn left onto Main St");
    run_ms(100);
    CHECK(find_label(lv_screen_active(), "Keep right at the fork", false) != NULL);   /* held */
    run_ms(4000);
    nova_panel_set_route_line(np, "In 100 m \xC2\xB7 Turn left onto Main St");
    run_ms(100);
    CHECK(find_label(lv_screen_active(), "In 100 m \xC2\xB7 Turn left onto Main St", false) != NULL);
    nova_panel_set_route_line(np, NULL);
    run_ms(100);

    CHECK_EQ(s_warnings, 0);
    lv_obj_delete(panel);   /* joins the map's worker before exit, as main does */
    DONE();
}
