#include "widgets/settings_sheet.h"
#include "cb_theme.h"
#include "live/live.h"
#include "screens/cockpit.h"
#include "widgets/ui_kit.h"
#include <pthread.h>
#include <stdio.h>
#include <string.h>

static lv_obj_t *s_chip, *s_overlay, *s_providers, *s_models, *s_failover, *s_status;
static live_llm_t s_llm;
static char s_provider[24], s_model[96];
static bool s_auto, s_model_picked;

/* Apply runs on a worker: the pipeline answers a switch to the on-device model only after loading it. */
static struct {
    pthread_t thread;
    volatile bool busy, done;
    bool ok;
    char provider[24], model[96], err[160];
    bool send_model;   /* else the pipeline keeps its model (a failover toggle must not re-pick one) */
    bool failover;
    live_llm_t now;
    lv_timer_t *poll;
} s_apply;

static const char *pretty(const char *id)
{
    if(!strcmp(id, "groq")) return "Groq";
    if(!strcmp(id, "openrouter")) return "OpenRouter";
    if(!strcmp(id, "cerebras")) return "Cerebras";
    if(!strcmp(id, "local")) return "On-device";
    return id;
}

static void chip_text(const live_llm_t *l)
{
    if(!s_chip) return;
    char t[140];
    snprintf(t, sizeof t, "%s \xC2\xB7 %s", pretty(l->provider), l->model[0] ? l->model : "default");
    cockpit_set_chip(s_chip, t, cb_col(CB_VIOLET));
}

static void on_llm(const live_llm_t *l)
{
    s_llm = *l;
    chip_text(l);
}

void settings_sheet_init(lv_obj_t *chip)
{
    s_chip = chip;
    live_on_llm(on_llm);
}

static void close_sheet(void)
{
    if(s_overlay) lv_obj_delete(s_overlay);
    s_overlay = s_providers = s_models = s_failover = s_status = NULL;
}

static lv_obj_t *pill(lv_obj_t *parent, const char *text, bool on, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(b, 18, 0);
    lv_obj_set_style_pad_ver(b, 10, 0);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, on ? cb_col(CB_CYAN) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b, on ? 60 : 18, 0);
    lv_obj_set_style_border_width(b, on ? 1 : 0, 0);
    lv_obj_set_style_border_color(b, cb_col(CB_CYAN), 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, 8, 0);
    lv_obj_set_clickable(b, true);
    ui_label(b, text, CB_F_BODY, on ? CB_TEXT_HI : CB_TEXT_2);
    if(cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    return b;
}

static int provider_index(const char *id)
{
    for(int i = 0; i < s_llm.n; i++)
        if(!strcmp(s_llm.p[i].id, id)) return i;
    return -1;
}

static void build_choices(void);

static void provider_click(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    snprintf(s_provider, sizeof s_provider, "%s", s_llm.p[i].id);
    /* keep the model if the new provider offers it, else take its first */
    bool has = false;
    for(int k = 0; k < s_llm.p[i].n_models; k++) has |= !strcmp(s_llm.p[i].models[k], s_model);
    if(!has) snprintf(s_model, sizeof s_model, "%s", s_llm.p[i].n_models ? s_llm.p[i].models[0] : "");
    build_choices();
}

static void model_click(lv_event_t *e)
{
    int k = (int)(intptr_t)lv_event_get_user_data(e);
    int i = provider_index(s_provider);
    if(i >= 0 && k < s_llm.p[i].n_models) snprintf(s_model, sizeof s_model, "%s", s_llm.p[i].models[k]);
    s_model_picked = true;
    build_choices();
}

static void failover_click(lv_event_t *e)
{
    LV_UNUSED(e);
    s_auto = !s_auto;
    build_choices();
}

static void build_choices(void)
{
    lv_obj_clean(s_providers);
    for(int i = 0; i < s_llm.n; i++) {
        lv_obj_t *b = pill(s_providers, pretty(s_llm.p[i].id), !strcmp(s_llm.p[i].id, s_provider), provider_click,
                           (void *)(intptr_t)i);
        lv_obj_t *dot = ui_dot(b, s_llm.p[i].ok ? CB_GREEN : CB_RED, false);
        lv_obj_move_to_index(dot, 0);
    }
    lv_obj_clean(s_models);
    int i = provider_index(s_provider);
    for(int k = 0; i >= 0 && k < s_llm.p[i].n_models; k++) {
        bool on = !strcmp(s_llm.p[i].models[k], s_model);
        lv_obj_t *row = ui_row(s_models, 12);
        lv_obj_set_style_pad_hor(row, 14, 0);
        lv_obj_set_style_pad_ver(row, 10, 0);
        lv_obj_set_style_radius(row, 12, 0);
        lv_obj_set_style_bg_color(row, on ? cb_col(CB_CYAN) : lv_color_white(), 0);
        lv_obj_set_style_bg_opa(row, on ? 40 : 10, 0);
        lv_obj_set_clickable(row, true);
        ui_dot(row, on ? CB_CYAN : CB_TEXT_LO, false);
        ui_label(row, s_llm.p[i].models[k], CB_F_BODY, on ? CB_TEXT_HI : CB_TEXT_2);
        lv_obj_add_event_cb(row, model_click, LV_EVENT_CLICKED, (void *)(intptr_t)k);
    }
    lv_obj_clean(s_failover);
    pill(s_failover, s_auto ? "Automatic failover: on" : "Automatic failover: off", s_auto, failover_click, NULL);
    lv_obj_t *why = ui_row(s_failover, 8);
    bool ready = s_auto && s_llm.failover_ready;
    ui_dot(why, ready ? CB_GREEN : CB_TEXT_LO, false);
    ui_label(why,
             !s_auto ? "off: a failing provider ends the turn"
             : ready ? "backup ready: a failing provider hands the turn to the other one"
                     : "no backup provider ready (needs the other provider's key; not for On-device)",
             CB_F_LABEL, CB_TEXT_LO);
}

static void cancel_click(lv_event_t *e)
{
    LV_UNUSED(e);
    if(!s_apply.busy) close_sheet();   /* a switch in flight finishes first */
}

static void *apply_worker(void *arg)
{
    LV_UNUSED(arg);
    s_apply.ok = live_llm_apply(s_apply.provider, s_apply.send_model ? s_apply.model : NULL, s_apply.failover,
                                &s_apply.now, s_apply.err, sizeof s_apply.err);
    s_apply.done = true;
    return NULL;
}

static void apply_poll(lv_timer_t *t)
{
    if(!s_apply.done) return;
    pthread_join(s_apply.thread, NULL);
    lv_timer_delete(t);
    s_apply.busy = false;
    if(s_apply.now.provider[0]) on_llm(&s_apply.now);
    if(s_apply.ok) close_sheet();
    else if(s_status) lv_label_set_text_fmt(s_status, "Couldn't change it: %s", s_apply.err);
}

static void apply_click(lv_event_t *e)
{
    LV_UNUSED(e);
    if(s_apply.busy) return;
    snprintf(s_apply.provider, sizeof s_apply.provider, "%s", s_provider);
    snprintf(s_apply.model, sizeof s_apply.model, "%s", s_model);
    /* a new provider needs one of its own models (the shown pick); otherwise only a tapped model is sent */
    s_apply.send_model = s_model_picked || strcmp(s_provider, s_llm.provider) != 0;
    s_apply.failover = s_auto;
    s_apply.busy = true;
    s_apply.done = false;
    if(pthread_create(&s_apply.thread, NULL, apply_worker, NULL) != 0) {
        s_apply.busy = false;
        lv_label_set_text(s_status, "Couldn't change it: no worker thread");
        return;
    }
    lv_label_set_text_fmt(s_status, "Switching to %s \xC2\xB7 %s\xE2\x80\xA6", pretty(s_provider), s_model);
    s_apply.poll = lv_timer_create(apply_poll, 100, NULL);
}

static lv_obj_t *section(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_add_style(l, &cb_st_label, 0);
    lv_label_set_text(l, text);
    return l;
}

void settings_sheet_open(void)
{
    if(s_overlay) return;
    /* over the whole cockpit panel (the chip's top-most ancestor below the screen), so snapshots include it */
    lv_obj_t *root = s_chip ? s_chip : lv_screen_active();
    while(lv_obj_get_parent(root) && lv_obj_get_parent(root) != lv_screen_active()) root = lv_obj_get_parent(root);
    s_overlay = lv_obj_create(root);
    lv_obj_set_floating(s_overlay, true);
    lv_obj_remove_style_all(s_overlay);
    lv_obj_set_size(s_overlay, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_overlay, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_overlay, 160, 0);
    lv_obj_set_clickable(s_overlay, true);   /* taps outside the card do nothing */

    lv_obj_t *card = ui_plain(s_overlay);
    lv_obj_set_size(card, 720, LV_SIZE_CONTENT);
    lv_obj_center(card);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x12161e), 0);
    lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(card, lv_color_white(), 0);
    lv_obj_set_style_border_opa(card, 30, 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_radius(card, 24, 0);
    lv_obj_set_style_pad_all(card, 28, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(card, 16, 0);

    ui_label(card, "Nova \xC2\xB7 language model", CB_F_TITLE, CB_TEXT_HI);
    s_status = ui_label(card, "", CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_width(s_status, lv_pct(100));
    lv_label_set_long_mode(s_status, LV_LABEL_LONG_MODE_WRAP);

    char err[160] = "";
    bool have = live_linked() && live_llm_fetch(&s_llm, err, sizeof err);
    if(have) {
        snprintf(s_provider, sizeof s_provider, "%s", s_llm.provider);
        snprintf(s_model, sizeof s_model, "%s", s_llm.model);
        s_auto = s_llm.failover;
        s_model_picked = false;
        chip_text(&s_llm);
        lv_label_set_text_fmt(s_status, "Now: %s \xC2\xB7 %s", pretty(s_llm.provider), s_llm.model);
        section(card, "PROVIDER");
        s_providers = ui_row(card, 10);
        lv_obj_set_flex_flow(s_providers, LV_FLEX_FLOW_ROW_WRAP);
        section(card, "MODEL");
        s_models = ui_col(card, 6);
        s_failover = ui_col(card, 6);
        build_choices();
    }
    else if(!live_linked())
        lv_label_set_text(s_status, "Connect the cockpit to a live Nova pipeline (--live) to change the model.");
    else lv_label_set_text_fmt(s_status, "Settings unavailable: %s", err);

    lv_obj_t *buttons = ui_row(card, 12);
    lv_obj_set_flex_align(buttons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    pill(buttons, have ? "Cancel" : "Close", false, cancel_click, NULL);
    if(have) pill(buttons, "Apply", true, apply_click, NULL);
}
