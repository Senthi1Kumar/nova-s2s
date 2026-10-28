#include "app.h"
#include "nova/avatar.h"
#include "voice.h"
#include <stdio.h>
#include <string.h>

/* Nova Voice: the NovaBot face, an open mic with a Mute toggle, and a Stop button, over WS /v1/voice/stream.
   voice.c owns the socket, PortAudio and the VAD on worker threads; this page only reads a snapshot every 33 ms. */

#define DEFAULT_MODEL "qwen3-vl"

static struct {
    lv_obj_t *av, *state, *mic_lbl, *hint, *mic_btn, *mic_slash, *stop_btn, *dd, *you, *reply, *timings, *dev, *dev_dot;
    lv_obj_t *you_card, *reply_card;
    lv_point_precise_t slash_pts[2];
    lv_timer_t *timer;
    uint32_t seen_gen, seen_models;
    voice_state_t shown;
    int shown_muted, shown_level;
    char sel[64];
} P;

static const char *state_text(const voice_view_t *v)
{
    if(v->muted) return "Muted";
    switch(v->state) {
    case VOICE_THINKING: return "Thinking\xE2\x80\xA6";
    case VOICE_SPEAKING: return "Speaking";
    default: return "Listening\xE2\x80\xA6";
    }
}

static nv_activity_t activity_for(voice_state_t s)
{
    switch(s) {
    case VOICE_HEARD: return NV_LISTENING;
    case VOICE_THINKING: return NV_THINKING;
    case VOICE_SPEAKING: return NV_SPEAKING;
    default: return NV_IDLE;
    }
}

static void paint_mute(bool muted)
{
    lv_obj_set_style_bg_color(P.mic_btn, muted ? cb_col(CB_RED) : cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(P.mic_btn, muted ? 70 : LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(P.mic_btn, muted ? cb_col(CB_RED) : cb_col(CB_HAIRLINE), 0);
    lv_obj_set_hidden(P.mic_slash, !muted);
}

static void mic_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    voice_view_t v;
    voice_take(&v);
    voice_set_muted(!v.muted);
    paint_mute(!v.muted);
}

static void stop_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    voice_cancel();
}

static void model_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    lv_dropdown_get_selected_str(P.dd, P.sel, sizeof P.sel);
    voice_set_model(P.sel);
}

/* Microphone glyph from plain shapes (capsule, cup, stem), as the cockpit's bar draws it. */
static void mic_glyph(lv_obj_t *btn)
{
    lv_obj_t *g = ui_plain(btn);
    lv_obj_set_size(g, 14, 19);
    lv_obj_center(g);
    lv_obj_t *cap = ui_plain(g);
    lv_obj_set_size(cap, 6, 10);
    lv_obj_set_style_radius(cap, 3, 0);
    lv_obj_set_style_bg_color(cap, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_bg_opa(cap, LV_OPA_COVER, 0);
    lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_t *cup = ui_plain(g);
    lv_obj_set_size(cup, 12, 8);
    lv_obj_set_style_border_width(cup, 2, 0);
    lv_obj_set_style_border_color(cup, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_side(cup, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_LEFT | LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_set_style_radius(cup, 6, 0);
    lv_obj_align(cup, LV_ALIGN_TOP_MID, 0, 6);
    lv_obj_t *stem = ui_plain(g);
    lv_obj_set_size(stem, 2, 4);
    lv_obj_set_style_bg_color(stem, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_bg_opa(stem, LV_OPA_COVER, 0);
    lv_obj_align(stem, LV_ALIGN_BOTTOM_MID, 0, 0);
}

static lv_obj_t *round_btn(lv_obj_t *parent, int32_t w, int32_t h)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, w, h);
    lv_obj_set_style_radius(b, h / 2, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_bg_color(b, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_set_style_border_color(b, cb_col(CB_HAIRLINE), 0);
    return b;
}

static lv_obj_t *dropdown(lv_obj_t *parent, int32_t w)
{
    lv_obj_t *d = lv_dropdown_create(parent);
    lv_dropdown_set_options(d, DEFAULT_MODEL);
    lv_obj_set_width(d, w);
    lv_obj_set_style_text_font(d, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_bg_color(d, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(d, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_color(d, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(d, 1, 0);
    lv_obj_set_style_radius(d, 8, 0);
    lv_obj_set_style_text_font(d, &lv_font_montserrat_14, LV_PART_INDICATOR);
    lv_obj_set_style_text_color(d, cb_col(CB_TEXT_MID), LV_PART_INDICATOR);
    lv_obj_t *list = lv_dropdown_get_list(d);
    lv_obj_set_style_bg_color(list, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(list, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(list, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(list, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_border_color(list, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_bg_color(list, cb_col(CB_CYAN), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(list, cb_col(CB_BASE), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_add_event_cb(d, model_cb, LV_EVENT_VALUE_CHANGED, NULL);
    return d;
}

static lv_obj_t *bubble(lv_obj_t *parent, const char *caption, const char *placeholder, lv_obj_t **text)
{
    lv_obj_t *c = ui_card(parent);
    lv_obj_set_style_bg_color(c, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_pad_all(c, 14, 0);
    lv_obj_set_style_pad_row(c, 6, 0);
    lv_obj_set_width(c, lv_pct(48));
    lv_obj_set_flex_grow(c, 1);
    lv_obj_set_height(c, 150);
    lv_obj_t *cap = ui_label(c, caption, CB_F_LABEL, CB_TEXT_MID);
    lv_obj_set_style_text_letter_space(cap, 2, 0);
    lv_obj_t *t = ui_label(c, placeholder, CB_F_VALUE, CB_TEXT_LO);
    lv_obj_set_width(t, lv_pct(100));
    lv_label_set_long_mode(t, LV_LABEL_LONG_MODE_WRAP);
    *text = t;
    return c;
}

static void build(lv_obj_t *page)
{
    int32_t vres = lv_display_get_vertical_resolution(lv_display_get_default());
    int32_t box = vres * 36 / 100;
    if(box < 240) box = 240;
    if(box > 400) box = 400;

    lv_obj_t *card = app_card(page, NULL);
    lv_obj_set_width(card, lv_pct(100));
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(card, 10, 0);

    lv_obj_t *top = ui_row(card, 10);
    ui_icon(top, UI_ICON_CHIP, CB_TEXT_MID);
    ui_label(top, "Model", CB_F_BODY, CB_TEXT_MID);
    P.dd = dropdown(top, 260);
    ui_spacer(top);
    ui_icon(top, UI_ICON_SPEAKER, CB_TEXT_MID);
    P.dev_dot = ui_dot(top, CB_RED, false);
    P.dev = ui_label(top, "Connecting\xE2\x80\xA6", CB_F_LABEL, CB_TEXT_MID);

    P.av = avatar_create(card, box);
    P.state = ui_label(card, "Listening\xE2\x80\xA6", CB_F_TEMP, CB_TEXT_HI);
    P.hint = ui_label(card, "", CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_style_text_align(P.hint, LV_TEXT_ALIGN_CENTER, 0);

    lv_obj_t *ctl = ui_row(card, 14);
    lv_obj_set_width(ctl, LV_SIZE_CONTENT);
    P.mic_btn = round_btn(ctl, 56, 56);
    mic_glyph(P.mic_btn);
    P.mic_slash = lv_line_create(P.mic_btn);
    P.slash_pts[0] = (lv_point_precise_t){ 14, 14 };
    P.slash_pts[1] = (lv_point_precise_t){ 42, 42 };
    lv_line_set_points(P.mic_slash, P.slash_pts, 2);
    lv_obj_set_size(P.mic_slash, 56, 56);
    lv_obj_set_style_line_width(P.mic_slash, 3, 0);
    lv_obj_set_style_line_color(P.mic_slash, cb_col(CB_RED), 0);
    lv_obj_set_style_line_rounded(P.mic_slash, true, 0);
    lv_obj_set_hidden(P.mic_slash, true);
    lv_obj_add_event_cb(P.mic_btn, mic_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *ml = ui_label(ctl, "Mute", CB_F_VALUE, CB_TEXT_2);
    lv_obj_set_width(ml, 76);
    lv_obj_set_style_pad_right(ml, 14, 0);
    P.mic_lbl = ml;

    P.stop_btn = round_btn(ctl, 130, 56);
    lv_obj_set_style_bg_color(P.stop_btn, cb_col(CB_RED), 0);
    lv_obj_set_style_bg_opa(P.stop_btn, 70, 0);
    lv_obj_set_style_border_color(P.stop_btn, cb_col(CB_RED), 0);
    lv_obj_t *sq = ui_plain(P.stop_btn);
    lv_obj_set_size(sq, 14, 14);
    lv_obj_set_style_radius(sq, 3, 0);
    lv_obj_set_style_bg_color(sq, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_bg_opa(sq, LV_OPA_COVER, 0);
    lv_obj_align(sq, LV_ALIGN_LEFT_MID, 22, 0);
    lv_obj_t *sl = ui_label(P.stop_btn, "Stop", CB_F_VALUE, CB_TEXT_HI);
    lv_obj_align(sl, LV_ALIGN_LEFT_MID, 50, 0);
    lv_obj_add_event_cb(P.stop_btn, stop_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *row = app_flow(page, 14);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    P.you_card = bubble(row, "YOU", "Say something to Nova.", &P.you);
    P.reply_card = bubble(row, "NOVA", "", &P.reply);

    lv_obj_t *tcard = app_card(page, NULL);
    lv_obj_set_width(tcard, lv_pct(100));
    P.timings = ui_label(tcard, "ASR -    LLM TTFT -    LLM -    TTS -", CB_F_MONO, CB_TEXT_MID);

    voice_set_model(DEFAULT_MODEL);
}

/* Chat models from GET /v1/models (the 2 Hz snapshot), keeping the user's choice. */
static void tick(const net_snapshot_t *s)
{
    if(s->models_seq == P.seen_models || !s->models_ok) return;
    P.seen_models = s->models_seq;
    char opts[1024] = "";
    int n = 0, pick = 0;
    size_t len = 0;
    const char *want = P.sel[0] ? P.sel : DEFAULT_MODEL;
    for(int i = 0; i < s->n_models; i++) {
        if(strcmp(s->models[i].modality, "chat") != 0 && strcmp(s->models[i].modality, "vlm") != 0) continue;
        int w = snprintf(opts + len, sizeof opts - len, "%s%s", n ? "\n" : "", s->models[i].id);
        if(w < 0 || len + (size_t)w >= sizeof opts) break;
        len += (size_t)w;
        if(!strcmp(s->models[i].id, want)) pick = n;
        n++;
    }
    if(!n) return;
    lv_dropdown_set_options(P.dd, opts);
    lv_dropdown_set_selected(P.dd, (uint32_t)pick);
    model_cb(NULL);
}

static void timer_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    voice_view_t v;
    voice_take(&v);
    bool changed = v.state != P.shown || (int)v.muted != P.shown_muted;
    if(changed) {
        avatar_set_state(P.av, activity_for(v.state), v.muted);
        lv_label_set_text(P.state, state_text(&v));
        lv_obj_set_style_text_color(P.state, cb_col(v.muted ? CB_RED : v.state == VOICE_SPEAKING ? CB_CYAN : CB_TEXT_HI), 0);
        if((int)v.muted != P.shown_muted) {
            paint_mute(v.muted);
            lv_label_set_text(P.mic_lbl, v.muted ? "Unmute" : "Mute");
        }
        P.shown = v.state;
        P.shown_muted = v.muted;
        P.shown_level = -1;
    }
    int lv = v.muted ? 0 : v.level;
    if(lv != P.shown_level) {
        P.shown_level = lv;
        avatar_set_level(P.av, v.state == VOICE_SPEAKING, (uint8_t)lv);
    }
    if(v.gen != P.seen_gen) {
        P.seen_gen = v.gen;
        if(v.transcript[0]) {
            lv_label_set_text(P.you, v.transcript);
            lv_obj_set_style_text_color(P.you, cb_col(CB_TEXT_HI), 0);
        }
        lv_label_set_text(P.reply, v.reply);
        lv_obj_set_style_text_color(P.reply, cb_col(CB_TEXT_HI), 0);
        if(v.have_timings) {
            char a[16], b[16], c[16], d[16], buf[128];
            app_fmt_ms(a, sizeof a, v.asr_ms);
            app_fmt_ms(b, sizeof b, v.ttft_ms);
            app_fmt_ms(c, sizeof c, v.llm_ms);
            app_fmt_ms(d, sizeof d, v.tts_ms);
            snprintf(buf, sizeof buf, "ASR %s    LLM TTFT %s    LLM %s    TTS %s", a, b, c, d);
            lv_label_set_text(P.timings, buf);
            lv_obj_set_style_text_color(P.timings, cb_col(CB_TEXT_HI), 0);
        }
    }
    const char *hint = "";
    cb_color_t hc = CB_TEXT_MID;
    if(v.err[0]) { hint = v.err; hc = CB_RED; }
    else if(!v.connected) hint = "Connecting to the box\xE2\x80\xA6";
    else if(!v.mic_ok) { hint = "No microphone found"; hc = CB_ORANGE; }
    else if(!v.calibrated) hint = "Calibrating to the room noise\xE2\x80\xA6";
    else if(v.muted) hint = "The microphone is off. Tap the mic to unmute.";
    else if(v.state == VOICE_HEARD) hint = "Hearing you\xE2\x80\xA6";
    else if(v.state == VOICE_IDLE) hint = "Just talk. Nova listens when you speak.";
    if(strcmp(lv_label_get_text(P.hint), hint)) {
        lv_label_set_text(P.hint, hint);
        lv_obj_set_style_text_color(P.hint, cb_col(hc), 0);
    }
    char dev[160];
    snprintf(dev, sizeof dev, "%s", v.connected ? (v.mic_ok ? v.mic_name : "no microphone") : "Connecting\xE2\x80\xA6");
    if(strcmp(lv_label_get_text(P.dev), dev)) lv_label_set_text(P.dev, dev);
    lv_obj_set_style_bg_color(P.dev_dot, cb_col(v.connected && v.mic_ok ? CB_GREEN : CB_RED), 0);
}

static void show(bool on)
{
    if(on) {
        P.shown = (voice_state_t)-1;
        P.shown_muted = -1;
        P.seen_gen = (uint32_t)-1;
        voice_open();
        if(!P.timer) P.timer = lv_timer_create(timer_cb, 33, NULL);
        avatar_greet(P.av);
    }
    else {
        if(P.timer) {
            lv_timer_delete(P.timer);
            P.timer = NULL;
        }
        voice_close();
    }
}

const app_page_t page_voice = {
    .nav = "Voice AI", .icon = UI_ICON_MIC, .eyebrow = "EDGE AI / VOICE", .title = "Voice AI",
    .subtitle = "Talk to the box: speech recognition, the language model and speech synthesis, all on this device.",
    .build = build, .tick = tick, .show = show,
};
