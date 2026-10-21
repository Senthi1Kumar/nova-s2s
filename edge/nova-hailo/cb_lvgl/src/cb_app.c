#include "cb_app.h"
#include <stdio.h>
#include "cb_model.h"
#include "cb_sim.h"
#include "live/live.h"
#include "live/session_ctl.h"
#include "nova/avatar.h"
#include "nova/nova_mood.h"
#include "screens/cockpit.h"
#include "screens/dash.h"
#include "widgets/comfort.h"
#include "widgets/connected.h"
#include "widgets/drive.h"
#include "widgets/ui_kit.h"

static nova_fsm_t s_fsm;
static nova_bar_t *s_bar;
static nova_panel_t *s_panel;
static cb_demo_t s_demo;
static bool s_demo_on;
static lv_obj_t *s_pause_btn;
static nova_mood_t s_mood;
static av_mood_t s_mood_pushed;

static void pause_click(lv_event_t *e)
{
    LV_UNUSED(e);
    cb_app_toggle_demo_pause();
}

/* Side effects of an FSM step beyond the bar itself: stop the scripted turn on any abort; in live mode a tap's
   request goes to the pipeline. */
static void app_fx(unsigned fx, void *ud)
{
    LV_UNUSED(ud);
    if((fx & NV_FX_ABORT) && s_demo_on) cb_demo_abort(&s_demo, lv_tick_get());
    session_ctl_request(fx);
}

unsigned cb_app_event(nv_event_t ev, const nv_done_t *done)
{
    unsigned fx = nova_fsm_handle(&s_fsm, ev, done, lv_tick_get());
    cb_app_nova_apply(fx);
    if(ev == NV_EV_TAP_TUCK || ev == NV_EV_TAP_END) nova_panel_tuck(s_panel);   /* a card does not hold a tuck */
    app_fx(fx, NULL);
    return fx;
}

/* Demo io. The runner handles NV_FX_ABORT from its own events itself, so emit does not call app_fx. */
static unsigned demo_emit(void *ctx, nv_event_t ev, const nv_done_t *done)
{
    LV_UNUSED(ctx);
    unsigned fx = nova_fsm_handle(&s_fsm, ev, done, lv_tick_get());
    cb_app_nova_apply(fx);
    return fx;
}
/* The demo talks without a pipeline, so its lines feed the mood as the frames would. */
static void demo_say(void *ctx, const char *text, bool user)
{
    LV_UNUSED(ctx);
    cb_app_nova_mood_event(user ? NM_SPEECH_STARTED : NM_REPLY_DELTA);
    cb_app_nova_text(text, user);
}
static void demo_apply(void *ctx, int id) { LV_UNUSED(ctx); cb_demo_apply_to_model(id); }
static bool demo_live(void *ctx) { LV_UNUSED(ctx); return s_fsm.activity != NV_IDLE; }

static void temp_step_sim(bool driver, int32_t delta_dc)
{
    cb_model_t *m = cb_model();
    cb_model_set_temp(driver, lv_subject_get_int(driver ? m->driver_temp_dc : m->passenger_temp_dc) + delta_dc);
}

static void push_mood(lv_obj_t *av, void *ud) { avatar_set_mood(av, *(const av_mood_t *)ud); }
static void publish_mood(av_mood_t mood)
{
    s_mood_pushed = mood;
    avatar_set_default_mood(mood);
    avatar_for_each(push_mood, &mood);
}
static void push_head(lv_obj_t *av, void *ud) { avatar_set_head(av, *(const av_head_t *)ud); }

static void tick_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    uint32_t now = lv_tick_get();
    av_mood_t mood = nova_mood_get(&s_mood, now);
    if(mood != s_mood_pushed) publish_mood(mood);
    unsigned fx = nova_fsm_handle(&s_fsm, NV_EV_TICK, NULL, now);
    cb_app_nova_apply(fx);
    if(s_demo_on) cb_demo_tick(&s_demo, now);
}

void cb_app_build(lv_obj_t *panel, const cb_app_cfg_t *cfg)
{
    cb_theme_set_reduce_motion(cfg->reduce_motion);
    cb_model_init();
    bool live = cfg->live_url || cfg->replay;
    if(!live) cb_sim_start(1);
    nova_fsm_init(&s_fsm, NULL);
    s_fsm.remote = live;   /* before the first nova.fsm frame too: a tap must not fake a local mute */

    lv_obj_t *dock_mid = NULL, *nova_slot = NULL;
    if(cfg->layout == CB_LAYOUT_B || cfg->layout == CB_LAYOUT_V2) {
        lv_obj_set_size(panel, lv_pct(100), lv_pct(100));
        cockpit_hooks_t hooks = { .temp_step = live ? live_temp_step : temp_step_sim };
        if(cfg->layout == CB_LAYOUT_B) dock_mid = cockpit_create(panel, &hooks)->dock_mid;
        else nova_slot = cockpit_create_v2(panel, &hooks)->nova_slot;
    }
    else {
        dash_t dash = dash_create(panel);
        drive_build(dash.drive);
        comfort_build(dash.comfort);
        connected_build(dash.connected);
        s_pause_btn = dash.pause_btn;
        lv_obj_add_event_cb(s_pause_btn, pause_click, LV_EVENT_CLICKED, NULL);
        dock_mid = dash.dock_mid;
    }

    if(nova_slot || (dock_mid && cfg->layout == CB_LAYOUT_B)) {
        s_panel = nova_slot ? nova_panel_create(nova_slot, &s_fsm, app_fx, NULL)
                            : nova_panel_create_dock(cockpit()->nova_dock, &s_fsm, app_fx, NULL);
        nova_panel_set_reduce_motion(s_panel, cfg->reduce_motion);
        nova_panel_set_max_height(s_panel, cockpit()->nova_max_h);
    }
    else {
        s_bar = nova_bar_create(dock_mid, &s_fsm, app_fx, NULL);
        lv_obj_center(nova_bar_obj(s_bar));
        nova_bar_set_reduce_motion(s_bar, cfg->reduce_motion);
    }

    s_demo_on = cfg->demo && !live;
    if(s_demo_on) {
        cb_demo_io_t io = { NULL, demo_emit, demo_say, demo_apply, demo_live };
        cb_demo_init(&s_demo, cb_demo_script, cb_demo_script_len, io, lv_tick_get(), 2500);
    }
    nova_mood_init(&s_mood, lv_tick_get());
    publish_mood(nova_mood_get(&s_mood, lv_tick_get()));
    lv_timer_create(tick_cb, 50, NULL);
    if(cfg->live_url && !live_start(cfg->live_url)) fprintf(stderr, "cb_lvgl: live link not started\n");
    if(cfg->replay && !live_start_replay(cfg->replay, cfg->replay_speed, true))
        fprintf(stderr, "cb_lvgl: replay not started\n");
}

void cb_app_toggle_demo_pause(void)
{
    if(!s_demo_on) return;
    cb_demo_set_paused(&s_demo, !s_demo.paused);
    if(s_pause_btn) lv_label_set_text(ui_chip_label(s_pause_btn), s_demo.paused ? "Resume demo" : "Pause demo");
}

nova_fsm_t *cb_app_fsm(void) { return &s_fsm; }

nova_bar_t *cb_app_bar(void) { return s_bar; }

nova_panel_t *cb_app_panel(void) { return s_panel; }

void cb_app_nova_apply(unsigned fx)
{
    if(!fx) return;
    if(s_panel) nova_panel_apply(s_panel, fx);
    else if(s_bar) nova_bar_apply(s_bar, fx);
}

void cb_app_nova_text(const char *text, bool user)
{
    if(s_panel) nova_panel_set_text(s_panel, text, user);
    else if(s_bar) nova_bar_set_text(s_bar, text, user);
}

void cb_app_nova_stream(const char *text)
{
    if(s_panel) nova_panel_stream_text(s_panel, text);
    else if(s_bar) nova_bar_stream_text(s_bar, text);
}

void cb_app_nova_pending(bool pending)
{
    if(s_panel) nova_panel_set_pending(s_panel, pending);
    else nova_bar_set_pending(s_bar, pending);
}

void cb_app_nova_card(const nova_card_view_t *card)
{
    if(s_panel) nova_panel_set_card(s_panel, card);
}

void cb_app_nova_greet(void)
{
    if(s_panel) nova_panel_greet(s_panel);
}

void cb_app_nova_route_line(const char *line)
{
    if(s_panel) nova_panel_set_route_line(s_panel, line);
}

void cb_app_nova_guidance(const char *text)
{
    if(s_panel) nova_panel_guidance(s_panel, text);
}

cb_demo_t *cb_app_demo(void) { return &s_demo; }

void cb_app_nova_mood_event(nm_event_t ev)
{
    nova_mood_event(&s_mood, ev, lv_tick_get());
}

static void push_duck(lv_obj_t *av, void *ud) { avatar_set_duck(av, *(const float *)ud); }
void cb_app_nova_duck(float gain)
{
    avatar_for_each(push_duck, &gain);
}

void cb_app_set_avatar(const char *avatar_setting, const char *voice_id)
{
    av_head_t head = av_head_parse(avatar_setting);
    if(head >= AV_HEAD_COUNT) head = av_head_for_voice(voice_id);
    avatar_set_default_head(head);
    avatar_for_each(push_head, &head);
}
