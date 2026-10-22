#include "widgets/settings_sheet.h"
#include "cb_app.h"
#include "cb_theme.h"
#include "live/live.h"
#include "widgets/ui_kit.h"
#include <pthread.h>
#include <stdio.h>
#include <string.h>

#define OFFLINE_LINE "Connect to Nova to change settings"

static lv_obj_t *s_root, *s_offline, *s_mode, *s_providers, *s_models, *s_failover, *s_llm_status, *s_voices,
    *s_speeds, *s_voice_status, *s_avatars, *s_apply;
static live_llm_t s_llm;
static live_tts_t s_tts;
static char s_provider[24], s_model[96];
static bool s_auto, s_model_picked, s_have_llm, s_have_tts, s_was_linked;
static lv_timer_t *s_watch;
static live_tts_t s_confirmed;   /* the voice settings the pipeline last reported */
static uint32_t s_next_fetch;
static int s_fetch_fails;   /* back-off step: 5 s, 10 s, then 30 s */
static bool s_test;
static int s_test_jobs;
/* One voice, speed or avatar choice made while a job runs; the latest tap per field wins. */
static struct { bool voice, speed, avatar; char v[48], s[8], a[12]; } s_pend;

typedef enum { JOB_FETCH, JOB_LLM, JOB_TTS, JOB_PREVIEW } job_kind_t;

/* Every blocking call runs on a worker: the pipeline answers a switch only after loading the model or voice. */
static struct {
    pthread_t thread;
    volatile bool busy, done;
    job_kind_t kind;
    bool ok, llm_ok, tts_ok, tts_unsupported;   /* unsupported: answered, but without voice settings */
    char provider[24], model[96], voice[48], speed[8], avatar[12], err[160];
    bool send_model, failover, send_voice, send_speed, send_avatar;
    live_llm_t llm;
    live_tts_t tts;
    lv_timer_t *poll;
} s_job;

static const struct { const char *id, *label; } k_heads[] = {
    { "voice", "Match the voice" }, { "pebble", "Pebble" }, { "capsule", "Capsule" }, { "dome", "Dome" },
    { "drop", "Drop" }, { "cloud", "Cloud" }, { "gem", "Gem" }, { "visor", "Visor" }, { "eyes", "Just Eyes" },
};
static const struct { const char *id, *label; } k_speeds[] = {
    { "slow", "Slow" }, { "normal", "Normal" }, { "fast", "Fast" }, { "faster", "Faster" },
};
#define N_HEADS ((int)(sizeof k_heads / sizeof k_heads[0]))
#define N_SPEEDS ((int)(sizeof k_speeds / sizeof k_speeds[0]))

static const char *pretty(const char *id)
{
    if(!strcmp(id, "groq")) return "Groq";
    if(!strcmp(id, "openrouter")) return "OpenRouter";
    if(!strcmp(id, "cerebras")) return "Cerebras";
    if(!strcmp(id, "local")) return "Local Hailo";
    return id;
}

static bool is_local(const char *id) { return !strcmp(id, "local"); }
static bool is_cloud_choice(const char *id) { return !is_local(id) && strcmp(id, "cerebras") != 0; }

static bool enabled(void) { return s_test || live_linked(); }

static lv_obj_t *pill(lv_obj_t *parent, const char *text, bool on, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(b, 14, 0);
    lv_obj_set_style_pad_ver(b, 8, 0);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, on ? cb_col(CB_CYAN) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b, on ? 60 : 18, 0);
    lv_obj_set_style_border_width(b, on ? 1 : 0, 0);
    lv_obj_set_style_border_color(b, cb_col(CB_CYAN), 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, 8, 0);
    lv_obj_set_clickable(b, true);
    lv_obj_set_style_opa(b, LV_OPA_40, LV_STATE_DISABLED);
    if(on) lv_obj_add_state(b, LV_STATE_CHECKED);
    if(!enabled()) lv_obj_add_state(b, LV_STATE_DISABLED);
    ui_label(b, text, CB_F_BODY, on ? CB_TEXT_HI : CB_TEXT_2);
    if(cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    return b;
}

static const char *human_err(const char *code)
{
    static const struct { const char *code, *text; } k[] = {
        { "unknown_voice", "That voice isn't installed" }, { "voice_load_failed", "Couldn't load that voice" },
        { "no_voice_choice", "Voice choice needs Piper" }, { "starting", "Nova is starting \xE2\x80\x94 try again" },
        { "unknown_speed", "Unknown speed" }, { "speaking", "Nova is speaking \xE2\x80\x94 try again" },
        { "bad_tts", "Couldn't read that choice" }, { "unknown_avatar", "Unknown avatar" },
        { "bad_json", "Couldn't read that choice" }, { "not_ready", "Voice isn't ready yet" },
        { "voice_too_slow", "Too slow on this Pi at that speed" },
    };
    for(size_t i = 0; i < sizeof k / sizeof k[0]; i++)
        if(!strcmp(code, k[i].code)) return k[i].text;
    return code;
}

static lv_obj_t *wrap_row(lv_obj_t *parent)
{
    lv_obj_t *r = ui_row(parent, 8);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_style_pad_row(r, 8, 0);
    return r;
}

static lv_obj_t *section(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_add_style(l, &cb_st_label, 0);
    lv_label_set_text(l, text);
    return l;
}

static lv_obj_t *status_line(lv_obj_t *parent)
{
    lv_obj_t *l = ui_label(parent, "", CB_F_LABEL, CB_TEXT_MID);
    lv_obj_set_width(l, lv_pct(100));
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    return l;
}

static void set_status(lv_obj_t *l, const char *fmt, const char *a, const char *b)
{
    if(l) lv_label_set_text_fmt(l, fmt, a, b);
}

/* ---------- language model ---------- */

static int provider_index(const char *id)
{
    for(int i = 0; i < s_llm.n; i++)
        if(!strcmp(s_llm.p[i].id, id)) return i;
    return -1;
}

static void build_llm(void);

static void mode_click(lv_event_t *e)
{
    bool cloud = (bool)(intptr_t)lv_event_get_user_data(e);
    if(!enabled()) return;
    if(!cloud) snprintf(s_provider, sizeof s_provider, "%s", "local");
    else if(is_local(s_provider)) {
        for(int i = 0; i < s_llm.n; i++)
            if(is_cloud_choice(s_llm.p[i].id)) {
                snprintf(s_provider, sizeof s_provider, "%s", s_llm.p[i].id);
                snprintf(s_model, sizeof s_model, "%s", s_llm.p[i].n_models ? s_llm.p[i].models[0] : "");
                s_model_picked = true;
                break;
            }
    }
    build_llm();
}

static void provider_click(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if(!enabled()) return;
    snprintf(s_provider, sizeof s_provider, "%s", s_llm.p[i].id);
    /* keep the model if the new provider offers it, else take its first */
    bool has = false;
    for(int k = 0; k < s_llm.p[i].n_models; k++) has |= !strcmp(s_llm.p[i].models[k], s_model);
    if(!has) snprintf(s_model, sizeof s_model, "%s", s_llm.p[i].n_models ? s_llm.p[i].models[0] : "");
    build_llm();
}

static void model_click(lv_event_t *e)
{
    int k = (int)(intptr_t)lv_event_get_user_data(e);
    int i = provider_index(s_provider);
    if(!enabled()) return;
    if(i >= 0 && k < s_llm.p[i].n_models) snprintf(s_model, sizeof s_model, "%s", s_llm.p[i].models[k]);
    s_model_picked = true;
    build_llm();
}

static void failover_click(lv_event_t *e)
{
    LV_UNUSED(e);
    if(!enabled()) return;
    s_auto = !s_auto;
    build_llm();
}

static bool job_start(job_kind_t kind);
static void update_apply(void);

static void apply_click(lv_event_t *e)
{
    LV_UNUSED(e);
    if(!enabled() || !s_have_llm) return;
    if(s_job.busy) {
        set_status(s_llm_status, "%s%s", "One moment\xE2\x80\xA6", "");
        return;
    }
    s_job.kind = JOB_LLM;
    snprintf(s_job.provider, sizeof s_job.provider, "%s", s_provider);
    snprintf(s_job.model, sizeof s_job.model, "%s", s_model);
    /* a new cloud provider needs one of its own models (the shown pick); otherwise only a tapped model is sent */
    s_job.send_model = !is_local(s_provider) && (s_model_picked || strcmp(s_provider, s_llm.provider) != 0);
    s_job.failover = s_auto;
    if(job_start(JOB_LLM)) set_status(s_llm_status, "Switching to %s%s\xE2\x80\xA6", pretty(s_provider), "");
}

static void build_llm(void)
{
    if(!s_root) return;
    lv_obj_clean(s_mode);
    bool local = is_local(s_provider);
    pill(s_mode, "Local Hailo", local, mode_click, (void *)(intptr_t)0);
    pill(s_mode, "Cloud", !local, mode_click, (void *)(intptr_t)1);

    lv_obj_clean(s_providers);
    lv_obj_clean(s_models);
    lv_obj_clean(s_failover);
    lv_obj_set_hidden(s_providers, local);
    lv_obj_set_hidden(s_models, local);
    lv_obj_set_hidden(s_failover, local && !s_auto);
    if(!local) {
        for(int i = 0; i < s_llm.n; i++) {
            if(!is_cloud_choice(s_llm.p[i].id)) continue;
            lv_obj_t *b = pill(s_providers, pretty(s_llm.p[i].id), !strcmp(s_llm.p[i].id, s_provider), provider_click,
                               (void *)(intptr_t)i);
            lv_obj_t *dot = ui_dot(b, s_llm.p[i].ok ? CB_GREEN : CB_RED, false);
            lv_obj_move_to_index(dot, 0);
        }
        int i = provider_index(s_provider);
        for(int k = 0; i >= 0 && k < s_llm.p[i].n_models; k++) {
            bool on = !strcmp(s_llm.p[i].models[k], s_model);
            lv_obj_t *row = ui_row(s_models, 12);
            lv_obj_set_style_pad_hor(row, 14, 0);
            lv_obj_set_style_pad_ver(row, 8, 0);
            lv_obj_set_style_radius(row, 12, 0);
            lv_obj_set_style_bg_color(row, on ? cb_col(CB_CYAN) : lv_color_white(), 0);
            lv_obj_set_style_bg_opa(row, on ? 40 : 10, 0);
            lv_obj_set_style_opa(row, LV_OPA_40, LV_STATE_DISABLED);
            lv_obj_set_clickable(row, true);
            if(on) lv_obj_add_state(row, LV_STATE_CHECKED);
            if(!enabled()) lv_obj_add_state(row, LV_STATE_DISABLED);
            ui_dot(row, on ? CB_CYAN : CB_TEXT_LO, false);
            lv_obj_t *l = ui_label(row, s_llm.p[i].models[k], CB_F_BODY, on ? CB_TEXT_HI : CB_TEXT_2);
            lv_obj_set_flex_grow(l, 1);
            lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
            lv_obj_add_event_cb(row, model_click, LV_EVENT_CLICKED, (void *)(intptr_t)k);
        }
    }
    if(!local || s_auto) {
        pill(s_failover, s_auto ? "Automatic failover: on" : "Automatic failover: off", s_auto, failover_click, NULL);
        lv_obj_t *why = ui_row(s_failover, 8);
        bool ready = s_auto && s_llm.failover_ready;
        ui_dot(why, ready ? CB_GREEN : CB_TEXT_LO, false);
        lv_obj_t *t = ui_label(why,
                 !s_auto ? "off: a failing provider ends the turn"
                 : ready ? "backup ready: a failing provider hands the turn to the other one"
                         : "no backup provider ready (needs the other provider's key)",
                 CB_F_LABEL, CB_TEXT_LO);
        lv_obj_set_flex_grow(t, 1);
        lv_label_set_long_mode(t, LV_LABEL_LONG_MODE_WRAP);
    }
}

static void adopt_llm(const live_llm_t *l)
{
    s_llm = *l;
    s_have_llm = true;
    snprintf(s_provider, sizeof s_provider, "%s", l->provider);
    snprintf(s_model, sizeof s_model, "%s", l->model);
    s_auto = l->failover;
    s_model_picked = false;
    update_apply();
}

static void on_llm(const live_llm_t *l)
{
    adopt_llm(l);
    if(s_root && s_llm_status && !s_job.busy)
        lv_label_set_text_fmt(s_llm_status, "Now: %s \xC2\xB7 %s", pretty(l->provider), l->model[0] ? l->model : "default");
    build_llm();
}

/* ---------- voice and avatar ---------- */

static void build_voice(void);
static void build_avatar(void);

static void set_current_avatar(void)
{
    cb_app_set_avatar(s_tts.avatar[0] ? s_tts.avatar : NULL, s_tts.voice[0] ? s_tts.voice : NULL);
}

/* A choice the pipeline refused or never got goes back to what it last reported, but only where nothing newer
   (another tap, a nova.settings frame) has changed the field since. */
static void revert_fields(bool voice, const char *v, bool speed, const char *sp, bool avatar, const char *a)
{
    if(voice && !strcmp(s_tts.voice, v)) snprintf(s_tts.voice, sizeof s_tts.voice, "%s", s_confirmed.voice);
    if(speed && !strcmp(s_tts.speed, sp)) snprintf(s_tts.speed, sizeof s_tts.speed, "%s", s_confirmed.speed);
    if(avatar && !strcmp(s_tts.avatar, a)) snprintf(s_tts.avatar, sizeof s_tts.avatar, "%s", s_confirmed.avatar);
}

static bool tts_start(bool sv, const char *voice, bool ss, const char *speed, bool sa, const char *avatar)
{
    s_job.send_voice = sv;
    s_job.send_speed = ss;
    s_job.send_avatar = sa;
    snprintf(s_job.voice, sizeof s_job.voice, "%s", sv ? voice : "");
    snprintf(s_job.speed, sizeof s_job.speed, "%s", ss ? speed : "");
    snprintf(s_job.avatar, sizeof s_job.avatar, "%s", sa ? avatar : "");
    if(!job_start(JOB_TTS)) {
        revert_fields(sv, s_job.voice, ss, s_job.speed, sa, s_job.avatar);
        return false;
    }
    set_status(s_voice_status, "%s%s", sv ? "Loading the voice\xE2\x80\xA6" : "Saving\xE2\x80\xA6", "");
    return true;
}

static bool tts_job(const char *voice, const char *speed, const char *avatar)
{
    if(!enabled()) return false;
    if(voice) snprintf(s_tts.voice, sizeof s_tts.voice, "%s", voice);
    if(speed) snprintf(s_tts.speed, sizeof s_tts.speed, "%s", speed);
    if(avatar) snprintf(s_tts.avatar, sizeof s_tts.avatar, "%s", avatar);
    if(s_job.busy) {   /* sent as soon as the running job finishes */
        if(voice) { s_pend.voice = true; snprintf(s_pend.v, sizeof s_pend.v, "%s", voice); }
        if(speed) { s_pend.speed = true; snprintf(s_pend.s, sizeof s_pend.s, "%s", speed); }
        if(avatar) { s_pend.avatar = true; snprintf(s_pend.a, sizeof s_pend.a, "%s", avatar); }
        set_status(s_voice_status, "%s%s", "One moment\xE2\x80\xA6", "");
        return true;
    }
    return tts_start(voice != NULL, voice, speed != NULL, speed, avatar != NULL, avatar);
}

static void voice_click(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if(i < 0 || i >= s_tts.n) return;
    char id[48];
    snprintf(id, sizeof id, "%s", s_tts.v[i].id);
    tts_job(id, NULL, NULL);
    build_voice();
}

static void speed_click(lv_event_t *e)
{
    tts_job(NULL, k_speeds[(int)(intptr_t)lv_event_get_user_data(e)].id, NULL);
    build_voice();
}

static void preview_click(lv_event_t *e)
{
    LV_UNUSED(e);
    if(!enabled()) return;
    if(s_job.busy) {
        set_status(s_voice_status, "%s%s", "One moment\xE2\x80\xA6", "");
        return;
    }
    snprintf(s_job.voice, sizeof s_job.voice, "%s", s_tts.voice);
    snprintf(s_job.speed, sizeof s_job.speed, "%s", s_tts.speed);
    if(job_start(JOB_PREVIEW)) set_status(s_voice_status, "%s%s", "Previewing\xE2\x80\xA6", "");
}

static void build_voice(void)
{
    if(!s_root) return;
    lv_obj_clean(s_voices);
    if(s_have_tts && s_tts.n == 0) ui_label(s_voices, "Voice choice needs Piper", CB_F_BODY, CB_TEXT_MID);
    for(int i = 0; i < s_tts.n; i++)
        pill(s_voices, s_tts.v[i].label[0] ? s_tts.v[i].label : s_tts.v[i].id, !strcmp(s_tts.v[i].id, s_tts.voice),
             voice_click, (void *)(intptr_t)i);
    lv_obj_clean(s_speeds);
    if(s_have_tts && s_tts.n == 0) return;   /* no Piper voices: no speeds, nothing to preview */
    for(int i = 0; i < N_SPEEDS; i++)
        pill(s_speeds, k_speeds[i].label, !strcmp(s_tts.speed, k_speeds[i].id), speed_click, (void *)(intptr_t)i);
    pill(s_speeds, "Preview", false, preview_click, NULL);
}

static void head_click(lv_event_t *e)
{
    const char *id = k_heads[(int)(intptr_t)lv_event_get_user_data(e)].id;
    if(!enabled()) return;
    if(!tts_job(NULL, NULL, id)) return;
    set_current_avatar();
    build_avatar();
}

static void build_avatar(void)
{
    if(!s_root) return;
    lv_obj_clean(s_avatars);
    const char *cur = s_tts.avatar[0] ? s_tts.avatar : "voice";
    for(int i = 0; i < N_HEADS; i++)
        pill(s_avatars, k_heads[i].label, !strcmp(cur, k_heads[i].id), head_click, (void *)(intptr_t)i);
}

/* What the pipeline reports, with any choice still waiting to be sent laid over it. */
static void adopt_tts(const live_tts_t *t)
{
    s_tts = *t;
    s_confirmed = *t;
    s_have_tts = true;
    if(s_pend.voice) snprintf(s_tts.voice, sizeof s_tts.voice, "%s", s_pend.v);
    if(s_pend.speed) snprintf(s_tts.speed, sizeof s_tts.speed, "%s", s_pend.s);
    if(s_pend.avatar) snprintf(s_tts.avatar, sizeof s_tts.avatar, "%s", s_pend.a);
}

static void on_tts(const live_tts_t *t)
{
    adopt_tts(t);
    build_voice();
    build_avatar();
}

/* ---------- worker ---------- */

static void update_apply(void)
{
    if(!s_apply) return;
    if(enabled() && s_have_llm) lv_obj_remove_state(s_apply, LV_STATE_DISABLED);
    else lv_obj_add_state(s_apply, LV_STATE_DISABLED);
}

static void sync_link(void)
{
    if(!s_root) return;
    bool on = enabled();
    lv_obj_set_hidden(s_offline, on);
    update_apply();
    build_llm();
    build_voice();
    build_avatar();
}

static void *job_worker(void *arg)
{
    LV_UNUSED(arg);
    s_job.err[0] = 0;
    switch(s_job.kind) {
    case JOB_FETCH: {
        char e1[160] = "", e2[160] = "";
        s_job.llm_ok = live_llm_fetch(&s_job.llm, e1, sizeof e1);
        s_job.tts_ok = live_tts_fetch(&s_job.tts, e2, sizeof e2);
        s_job.ok = s_job.llm_ok || s_job.tts_ok;
        s_job.tts_unsupported = !s_job.tts_ok && strstr(e2, "too old") != NULL;
        snprintf(s_job.err, sizeof s_job.err, "%s", e1[0] ? e1 : e2);
        break;
    }
    case JOB_LLM:
        s_job.ok = s_job.llm_ok = live_llm_apply(s_job.provider, s_job.send_model ? s_job.model : NULL, s_job.failover,
                                                 &s_job.llm, s_job.err, sizeof s_job.err);
        break;
    case JOB_TTS:
        s_job.ok = s_job.tts_ok = live_tts_apply(s_job.send_voice ? s_job.voice : NULL, s_job.send_speed ? s_job.speed : NULL,
                                                 s_job.send_avatar ? s_job.avatar : NULL, &s_job.tts, s_job.err,
                                                 sizeof s_job.err);
        break;
    case JOB_PREVIEW:
        s_job.ok = live_tts_preview(s_job.voice, s_job.speed, s_job.err, sizeof s_job.err);
        break;
    }
    s_job.done = true;
    return NULL;
}

static void llm_now(void)
{
    lv_label_set_text_fmt(s_llm_status, "Now: %s \xC2\xB7 %s", pretty(s_llm.provider), s_llm.model[0] ? s_llm.model : "default");
}

static void send_pending(void)
{
    if(!s_pend.voice && !s_pend.speed && !s_pend.avatar) return;
    bool sv = s_pend.voice, ss = s_pend.speed, sa = s_pend.avatar;
    char v[48], sp[8], a[12];
    snprintf(v, sizeof v, "%s", s_pend.v);
    snprintf(sp, sizeof sp, "%s", s_pend.s);
    snprintf(a, sizeof a, "%s", s_pend.a);
    memset(&s_pend, 0, sizeof s_pend);
    if(!enabled()) {
        revert_fields(sv, v, ss, sp, sa, a);
        return;
    }
    if(!tts_start(sv, v, ss, sp, sa, a)) {
        set_status(s_voice_status, "Couldn't change it: %s%s", "no worker thread", "");
        set_current_avatar();
    }
    build_voice();
    build_avatar();
}

static void job_finish(void)
{
    s_job.busy = false;
    s_job.poll = NULL;
    if(!s_root) return;
    set_status(s_voice_status, "%s%s", "", "");
    switch(s_job.kind) {
    case JOB_FETCH:
        /* once known, the model settings change only by frames and Apply: a retry must not undo an unapplied pick */
        if(s_job.llm_ok && !s_have_llm) {
            adopt_llm(&s_job.llm);
            llm_now();
            build_llm();
        }
        else if(!s_job.llm_ok && !s_have_llm)
            set_status(s_llm_status, "Settings unavailable: %s%s", human_err(s_job.err), "");
        if(s_job.tts_ok) {
            adopt_tts(&s_job.tts);
            set_current_avatar();
        }
        else if(s_job.tts_unsupported && !s_have_tts) {
            memset(&s_tts, 0, sizeof s_tts);
            s_confirmed = s_tts;
            s_have_tts = true;   /* known, and empty: nothing more to fetch */
        }
        if(s_have_llm && s_have_tts) s_fetch_fails = 0;
        else {
            static const uint32_t k_backoff_ms[3] = { 5000, 10000, 30000 };
            s_next_fetch = lv_tick_get() + k_backoff_ms[s_fetch_fails < 3 ? s_fetch_fails : 2];
            s_fetch_fails++;
        }
        build_voice();
        build_avatar();
        break;
    case JOB_LLM:
        if(s_job.llm.provider[0]) adopt_llm(&s_job.llm);
        if(s_job.ok) llm_now();
        else set_status(s_llm_status, "Couldn't change it: %s%s", human_err(s_job.err), "");
        build_llm();
        break;
    case JOB_TTS:
        if(s_job.ok) adopt_tts(&s_job.tts);
        else {
            revert_fields(s_job.send_voice, s_job.voice, s_job.send_speed, s_job.speed, s_job.send_avatar, s_job.avatar);
            set_status(s_voice_status, "Couldn't change it: %s%s", human_err(s_job.err), "");
        }
        set_current_avatar();
        build_voice();
        build_avatar();
        break;
    case JOB_PREVIEW:
        if(!s_job.ok) set_status(s_voice_status, "Couldn't preview: %s%s", human_err(s_job.err), "");
        break;
    }
    send_pending();
}

static void job_poll(lv_timer_t *t)
{
    if(!s_job.done) return;
    pthread_join(s_job.thread, NULL);
    lv_timer_delete(t);
    job_finish();
}

static bool job_start(job_kind_t kind)
{
    s_job.kind = kind;
    s_job.busy = true;
    s_job.done = false;
    if(s_test) {
        s_test_jobs++;
        return true;
    }
    if(pthread_create(&s_job.thread, NULL, job_worker, NULL) != 0) {
        s_job.busy = false;
        set_status(kind == JOB_LLM ? s_llm_status : s_voice_status, "Couldn't change it: %s%s", "no worker thread", "");
        return false;
    }
    s_job.poll = lv_timer_create(job_poll, 100, NULL);
    return true;
}

/* The link comes and goes: controls follow it. Until the pipeline's settings are known a fetch runs whenever the
   worker is free, retried every 5 s after a failure. */
static void watch_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    bool now = enabled();
    if(now != s_was_linked) {
        s_was_linked = now;
        s_next_fetch = lv_tick_get();
        s_fetch_fails = 0;
        if(!now) memset(&s_pend, 0, sizeof s_pend);
        sync_link();
    }
    if(now && !s_job.busy && (!s_have_llm || !s_have_tts) && (int32_t)(lv_tick_get() - s_next_fetch) >= 0)
        job_start(JOB_FETCH);
}

static void root_deleted(lv_event_t *e)
{
    LV_UNUSED(e);
    if(s_watch) lv_timer_delete(s_watch);
    s_watch = NULL;
    s_root = s_offline = s_mode = s_providers = s_models = s_failover = s_llm_status = s_voices = s_speeds =
        s_voice_status = s_avatars = s_apply = NULL;
}

void settings_sheet_build(lv_obj_t *parent)
{
    s_root = parent;
    lv_obj_add_event_cb(parent, root_deleted, LV_EVENT_DELETE, NULL);
    live_on_llm(on_llm);
    live_on_tts(on_tts);

    s_offline = ui_label(parent, OFFLINE_LINE, CB_F_LABEL, CB_YELLOW);
    lv_obj_set_width(s_offline, lv_pct(100));
    lv_label_set_long_mode(s_offline, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_hidden(s_offline, enabled());

    section(parent, "LANGUAGE MODEL");
    s_mode = wrap_row(parent);
    s_providers = wrap_row(parent);
    s_models = ui_col(parent, 6);
    s_failover = ui_col(parent, 6);
    lv_obj_t *buttons = ui_row(parent, 12);
    s_apply = pill(buttons, "Apply", true, apply_click, NULL);
    s_llm_status = status_line(parent);
    lv_obj_set_flex_grow(s_llm_status, 0);

    section(parent, "VOICE");
    s_voices = wrap_row(parent);
    s_speeds = wrap_row(parent);
    s_voice_status = status_line(parent);

    section(parent, "AVATAR");
    s_avatars = wrap_row(parent);

    s_was_linked = enabled();
    s_next_fetch = lv_tick_get();
    if(!s_have_llm) snprintf(s_provider, sizeof s_provider, "%s", "local");
    build_llm();
    build_voice();
    build_avatar();
    s_watch = lv_timer_create(watch_cb, 500, NULL);
    update_apply();
}

/* Test hooks: a link without a pipeline. Jobs start but wait for settings_sheet_test_finish(). */
void settings_sheet_test_mode(bool on)
{
    s_test = on;
    s_test_jobs = 0;
}

int settings_sheet_test_jobs(void) { return s_test_jobs; }
bool settings_sheet_test_busy(void) { return s_job.busy; }
int settings_sheet_test_kind(void) { return (int)s_job.kind; }

void settings_sheet_test_sent(char *voice, char *speed, char *avatar)
{
    snprintf(voice, 48, "%s", s_job.send_voice ? s_job.voice : "");
    snprintf(speed, 8, "%s", s_job.send_speed ? s_job.speed : "");
    snprintf(avatar, 12, "%s", s_job.send_avatar ? s_job.avatar : "");
}

void settings_sheet_test_finish(bool ok, const char *err, const live_llm_t *llm, const live_tts_t *tts)
{
    s_job.ok = ok;
    snprintf(s_job.err, sizeof s_job.err, "%s", err ? err : "");
    s_job.llm_ok = llm != NULL;
    s_job.tts_ok = tts != NULL;
    s_job.tts_unsupported = !tts && err && strstr(err, "too old");
    if(llm) s_job.llm = *llm;
    if(tts) s_job.tts = *tts;
    job_finish();
}

void settings_sheet_test_forget(bool llm, bool tts)
{
    if(llm) s_have_llm = false;
    if(tts) s_have_tts = false;
    s_fetch_fails = 0;
    s_next_fetch = lv_tick_get();
    update_apply();
}
