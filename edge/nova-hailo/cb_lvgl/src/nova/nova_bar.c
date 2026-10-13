#include "nova/nova_bar.h"
#include "nova/orb.h"
#include "cb_ambient.h"
#include "cb_theme.h"
#include <stdio.h>
#include <math.h>
#include <string.h>

#define BAR_H 72
#define BAR_W_OPEN 760
#define BAR_W_REST 108
#define ORB_D 52
#define FADE_OUT_MS 110   /* contents out before the layout swap */
#define SPRING_MS 600     /* width spring, long enough to settle */
#define FADE_IN_MS 240    /* contents back in while the spring settles */
#define FADE_IN_AT 45     /* ...starting at this percentage of the spring */

#define TXT_IDLE "Say \xE2\x80\x9CHey Nova\xE2\x80\x9D, or tap the orb"
#define TXT_MUTED "Mic off \xC2\xB7 Nova can\xE2\x80\x99t hear you, tap the mic to unmute"
#define TXT_LISTEN "Listening\xE2\x80\xA6"
#define TXT_THINK "Thinking\xE2\x80\xA6"
#define TXT_ANSWER "Waiting for your answer\xE2\x80\xA6"
#define TXT_FOLLOW "Listening for a follow-up\xE2\x80\xA6"

enum { PH_NONE, PH_FADE_OUT, PH_WIDTH };

struct nova_bar {
    lv_obj_t *bar, *orb, *text_col, *state_lbl, *text_lbl, *ctrls, *mic, *slash, *end, *tuck;
    nova_fsm_t *fsm;
    nova_bar_fx_cb_t on_fx;
    void *ud;
    nv_view_t shown, target;
    int phase;
    int32_t opa;               /* opacity of the fading contents (text column, ctrls, tuck) */
    int32_t w;                 /* current bar width, tracked by the width animation */
    int32_t w_from, w_to;      /* the running width spring */
    bool boosted;              /* holding a cb_frame_boost during the morph */
    bool reduce;
    nv_activity_t last_act;
    lv_point_precise_t slash_pts[2];
    char text[320];
    size_t len, typed;
    lv_timer_t *typer;
};

/* ---------- small builders ---------- */

static lv_obj_t *plain(lv_obj_t *parent)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_scrollable(o, false);
    lv_obj_set_clickable(o, false);
    return o;
}

/* Apple-style press: the whole button, glyph included, dips to 92 % quickly and springs back past 100 % on
   release. Style transitions run only on state changes, so idle buttons cost nothing. */
static void press_motion(lv_obj_t *btn)
{
    static const lv_style_prop_t props[] = { LV_STYLE_TRANSFORM_SCALE_X, LV_STYLE_TRANSFORM_SCALE_Y, 0 };
    static lv_style_transition_dsc_t press, release;
    static bool inited;
    if(!inited) {
        lv_style_transition_dsc_init(&press, props, lv_anim_path_ease_out, 90, 0, NULL);
        lv_style_transition_dsc_init(&release, props, lv_anim_path_overshoot, 320, 0, NULL);
        inited = true;
    }
    lv_obj_set_style_transform_pivot_x(btn, lv_pct(50), 0);
    lv_obj_set_style_transform_pivot_y(btn, lv_pct(50), 0);
    lv_obj_set_style_transform_scale_x(btn, 236, LV_STATE_PRESSED);
    lv_obj_set_style_transform_scale_y(btn, 236, LV_STATE_PRESSED);
    if(cb_reduce_motion()) return;
    lv_obj_set_style_transition(btn, &press, LV_STATE_PRESSED);
    lv_obj_set_style_transition(btn, &release, 0);
}

static lv_obj_t *round_button(lv_obj_t *parent, int32_t size, lv_color_t bg, lv_opa_t opa)
{
    lv_obj_t *b = plain(parent);
    lv_obj_set_size(b, size, size);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, bg, 0);
    lv_obj_set_style_bg_opa(b, opa, 0);
    lv_obj_set_clickable(b, true);
    press_motion(b);
    return b;
}

static lv_obj_t *glyph_label(lv_obj_t *parent, const char *txt, cb_color_t c)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, cb_font(CB_F_VALUE), 0);
    lv_obj_set_style_text_color(l, cb_col(c), 0);
    lv_obj_center(l);
    return l;
}

/* Microphone glyph from plain shapes: capsule, cup, stem (no icon font needed). */
static void mic_glyph(lv_obj_t *btn)
{
    lv_obj_t *g = plain(btn);
    lv_obj_set_size(g, 14, 19);
    lv_obj_center(g);
    lv_obj_t *cap = plain(g);
    lv_obj_set_size(cap, 6, 10);
    lv_obj_set_style_radius(cap, 3, 0);
    lv_obj_set_style_bg_color(cap, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_bg_opa(cap, LV_OPA_COVER, 0);
    lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_t *cup = plain(g);
    lv_obj_set_size(cup, 12, 8);
    lv_obj_set_style_border_width(cup, 2, 0);
    lv_obj_set_style_border_color(cup, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_side(cup, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_LEFT | LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_set_style_radius(cup, 6, 0);
    lv_obj_align(cup, LV_ALIGN_TOP_MID, 0, 6);
    lv_obj_t *stem = plain(g);
    lv_obj_set_size(stem, 2, 4);
    lv_obj_set_style_bg_color(stem, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_bg_opa(stem, LV_OPA_COVER, 0);
    lv_obj_align(stem, LV_ALIGN_BOTTOM_MID, 0, 0);
}

/* ---------- layout + tuck/expand ---------- */

static int32_t target_width(nv_view_t v) { return v == NV_VIEW_REST ? BAR_W_REST : BAR_W_OPEN; }

static void set_slash(nova_bar_t *b, int32_t size)
{
    b->slash_pts[0] = (lv_point_precise_t){ size / 4, size / 4 };
    b->slash_pts[1] = (lv_point_precise_t){ size - size / 4, size - size / 4 };
    lv_line_set_points(b->slash, b->slash_pts, 2);
    lv_obj_set_size(b->slash, size, size);
}

static void apply_layout(nova_bar_t *b, nv_view_t v)
{
    bool rest = v == NV_VIEW_REST;
    int32_t s = rest ? 28 : 36;
    lv_obj_set_hidden(b->text_col, rest);
    lv_obj_set_hidden(b->tuck, rest);
    lv_obj_set_flex_flow(b->ctrls, rest ? LV_FLEX_FLOW_COLUMN : LV_FLEX_FLOW_ROW);
    lv_obj_set_size(b->mic, s, s);
    lv_obj_set_size(b->end, s, s);
    set_slash(b, s);
    lv_obj_set_style_pad_left(b->bar, 10, 0);   /* same in both layouts, so the orb never jumps at the swap */
    lv_obj_set_style_pad_right(b->bar, rest ? 9 : 12, 0);
    lv_obj_set_style_pad_column(b->bar, rest ? 7 : 14, 0);
    b->shown = v;
}

/* Contents opacity; while they appear the text slides in from the orb and the controls grow from 86 %. */
static void fade_exec(void *var, int32_t v)
{
    nova_bar_t *b = var;
    b->opa = v;
    lv_opa_t o = (lv_opa_t)v;
    int32_t scale = 220 + 36 * v / 255;
    lv_obj_set_style_opa(b->text_col, o, 0);
    lv_obj_set_style_translate_x(b->text_col, -10 * (255 - v) / 255, 0);
    lv_obj_t *grow[2] = { b->ctrls, b->tuck };
    for(int i = 0; i < 2; i++) {
        lv_obj_set_style_opa(grow[i], o, 0);
        lv_obj_set_style_transform_scale_x(grow[i], scale, 0);
        lv_obj_set_style_transform_scale_y(grow[i], scale, 0);
    }
}

/* Damped spring for the width, t in seconds. Expanding is slightly underdamped (zeta 0.8, ~1.5 % overshoot, the
   bar "lands"); tucking is critically damped so the cluster never shrinks below its contents. */
static float spring(float t, bool expand)
{
    if(expand) {
        const float w0 = 15.7f, z = 0.8f, wd = 9.42f;   /* response 0.4 s; wd = w0 * sqrt(1 - z^2) */
        return 1.0f - expf(-z * w0 * t) * (cosf(wd * t) + z / 0.6f * sinf(wd * t));
    }
    const float w0 = 17.5f;   /* response 0.36 s */
    return 1.0f - expf(-w0 * t) * (1.0f + w0 * t);
}

static void set_width(nova_bar_t *b, int32_t w)
{
    b->w = w;
    lv_obj_set_width(b->bar, w);
}

static void width_exec(void *var, int32_t v)   /* v: progress 0..1000 through SPRING_MS */
{
    nova_bar_t *b = var;
    float x = v >= 1000 ? 1.0f : spring((float)v * SPRING_MS / 1.0e6f, b->w_to > b->w_from);
    set_width(b, b->w_from + (int32_t)lroundf((float)(b->w_to - b->w_from) * x));
}

static void run(nova_bar_t *b, lv_anim_exec_xcb_t exec, int32_t from, int32_t to, uint32_t ms,
                lv_anim_path_cb_t path, lv_anim_completed_cb_t done, uint32_t delay)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, b);
    lv_anim_set_exec_cb(&a, exec);
    lv_anim_set_values(&a, from, to);
    lv_anim_set_duration(&a, ms);
    lv_anim_set_delay(&a, delay);
    lv_anim_set_path_cb(&a, path);
    lv_anim_set_user_data(&a, b);
    lv_anim_set_completed_cb(&a, done);
    lv_anim_start(&a);
}

/* LVGL unlinks an animation before its completed callback, so "neither is left" means the sequence is over. */
static void hold_boost(nova_bar_t *b, bool on)
{
    if(on == b->boosted) return;
    b->boosted = on;
    cb_frame_boost(on);
}

static void settle(nova_bar_t *b)
{
    if(lv_anim_get(b, width_exec) || lv_anim_get(b, fade_exec)) return;
    b->phase = PH_NONE;
    hold_boost(b, false);
}

static void fade_in_done(lv_anim_t *a) { settle(lv_anim_get_user_data(a)); }

static void width_done(lv_anim_t *a)
{
    nova_bar_t *b = lv_anim_get_user_data(a);
    set_width(b, b->w_to);
    settle(b);
}

/* Contents are invisible here, so the layout swaps unseen; the width springs while they fade back in. */
static void start_width(nova_bar_t *b)
{
    apply_layout(b, b->target);
    b->phase = PH_WIDTH;
    b->w_from = b->w;
    b->w_to = target_width(b->target);
    bool moving = b->w_from != b->w_to;
    if(moving) run(b, width_exec, 0, 1000, SPRING_MS, lv_anim_path_linear, width_done, 0);
    run(b, fade_exec, b->opa, 255, FADE_IN_MS, lv_anim_path_ease_out, fade_in_done, moving ? SPRING_MS * FADE_IN_AT / 100 : 0);
}

static void fade_out_done(lv_anim_t *a) { start_width(lv_anim_get_user_data(a)); }

/* Fade the contents out, swap the layout, spring the width while the contents fade back in. Any phase can be
   interrupted: the next request starts from the current opacity and width, so taps mid-animation never jump. */
static void request_view(nova_bar_t *b, nv_view_t v)
{
    if(v == b->target && (b->phase != PH_NONE || (b->shown == v && b->w == target_width(v)))) return;
    lv_anim_delete(b, fade_exec);
    lv_anim_delete(b, width_exec);
    b->target = v;
    if(b->reduce) {
        apply_layout(b, v);
        set_width(b, target_width(v));
        fade_exec(b, 255);
        b->phase = PH_NONE;
        return;
    }
    b->phase = PH_FADE_OUT;
    hold_boost(b, true);   /* 60 fps for the morph; released when it settles */
    uint32_t ms = FADE_OUT_MS * (uint32_t)b->opa / 255;
    if(ms == 0) {
        start_width(b);
        return;
    }
    run(b, fade_exec, b->opa, 0, ms, lv_anim_path_ease_in, fade_out_done, 0);
}

/* ---------- text ---------- */

static void stop_typer(nova_bar_t *b)
{
    if(b->typer) {
        lv_timer_delete(b->typer);
        b->typer = NULL;
    }
}

static void set_static_text(nova_bar_t *b, const char *txt)
{
    stop_typer(b);
    lv_label_set_text(b->text_lbl, txt);
    lv_obj_set_style_text_color(b->text_lbl, cb_col(CB_TEXT_MID), 0);
}

static void typer_cb(lv_timer_t *t)
{
    nova_bar_t *b = lv_timer_get_user_data(t);
    for(int n = 0; n < 2 && b->typed < b->len; n++) {   /* advance two UTF-8 characters */
        b->typed++;
        while(b->typed < b->len && ((unsigned char)b->text[b->typed] & 0xC0) == 0x80) b->typed++;
    }
    char keep = b->text[b->typed];
    b->text[b->typed] = '\0';
    lv_label_set_text(b->text_lbl, b->text);
    b->text[b->typed] = keep;
    if(b->typed >= b->len) stop_typer(b);
}

void nova_bar_set_text(nova_bar_t *b, const char *text, bool user)
{
    stop_typer(b);
    snprintf(b->text, sizeof(b->text), user ? "You \xC2\xB7 %s" : "%s", text ? text : "");
    b->len = strlen(b->text);
    lv_obj_set_style_text_color(b->text_lbl, cb_col(user ? CB_TEXT_MID : CB_TEXT_HI), 0);
    if(b->reduce) {
        b->typed = b->len;
        lv_label_set_text(b->text_lbl, b->text);
        return;
    }
    b->typed = user ? strlen("You \xC2\xB7 ") : 0;
    b->typer = lv_timer_create(typer_cb, 26, b);
    typer_cb(b->typer);
}

void nova_bar_stream_text(nova_bar_t *b, const char *text)
{
    if(b->len == 0 || strncmp(b->text, text, b->len) != 0) {   /* a new reply, or the user's line was showing */
        nova_bar_set_text(b, text, false);
        return;
    }
    snprintf(b->text, sizeof(b->text), "%s", text);
    b->len = strlen(b->text);
    if(b->reduce) {
        b->typed = b->len;
        lv_label_set_text(b->text_lbl, b->text);
        return;
    }
    if(!b->typer && b->typed < b->len) b->typer = lv_timer_create(typer_cb, 26, b);
}

/* ---------- state ---------- */

static lv_color_t state_color(const nova_fsm_t *f)
{
    if(f->muted) return cb_col(CB_RED);
    switch(f->activity) {
        case NV_LISTENING: return cb_col(CB_CYAN);
        case NV_THINKING: return cb_col(CB_VIOLET);
        case NV_SPEAKING: return cb_col(CB_GREEN);
        default: return cb_col(CB_ORB_IDLE);
    }
}

static void refresh_state(nova_bar_t *b)
{
    const nova_fsm_t *f = b->fsm;
    lv_label_set_text(b->state_lbl, f->muted ? "MUTED" : nova_activity_name(f->activity));
    lv_obj_set_style_text_color(b->state_lbl, state_color(f), 0);
    orb_set_state(b->orb, f->activity, f->muted);

    lv_obj_set_hidden(b->slash, !f->muted);
    lv_obj_set_style_bg_color(b->mic, f->muted ? cb_col(CB_RED) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b->mic, f->muted ? 56 : 20, 0);
    lv_obj_set_style_border_width(b->mic, f->muted ? 1 : 0, 0);
    lv_obj_set_style_border_color(b->mic, cb_col(CB_RED), 0);

    nv_activity_t prev = b->last_act;
    if(f->muted) set_static_text(b, TXT_MUTED);
    else if(f->activity == NV_IDLE) set_static_text(b, TXT_IDLE);
    else if(f->activity == NV_LISTENING && (prev == NV_THINKING || prev == NV_SPEAKING))
        set_static_text(b, f->window_ms == f->cfg.answer_ms ? TXT_ANSWER : TXT_FOLLOW);
    else if(f->activity == NV_LISTENING && prev == NV_IDLE) set_static_text(b, TXT_LISTEN);
    else if(f->activity == NV_THINKING && prev == NV_LISTENING) set_static_text(b, TXT_THINK);
    b->last_act = f->activity;
}

void nova_bar_apply(nova_bar_t *b, unsigned fx)
{
    if(fx & (NV_FX_ACTIVITY | NV_FX_MUTE)) refresh_state(b);
    if(fx & NV_FX_VIEW) request_view(b, b->fsm->view);
    if(fx & NV_FX_CHIME) orb_bloom(b->orb);
}

/* ---------- taps ---------- */

static void handle(nova_bar_t *b, nv_event_t ev)
{
    unsigned fx = nova_fsm_handle(b->fsm, ev, NULL, lv_tick_get());
    nova_bar_apply(b, fx);
    if(b->on_fx) b->on_fx(fx, b->ud);
}

void nova_bar_set_pending(nova_bar_t *b, bool pending)
{
    if(!b) return;
    lv_obj_set_style_opa(b->mic, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
    lv_obj_set_style_opa(b->end, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
}

static void orb_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_ORB); }
static void mic_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_MIC); }
static void end_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_END); }
static void tuck_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_TUCK); }

static void bar_delete(lv_event_t *e)
{
    nova_bar_t *b = lv_event_get_user_data(e);
    lv_anim_delete(b, NULL);
    hold_boost(b, false);
    stop_typer(b);
    lv_free(b);
}

/* ---------- construction ---------- */

nova_bar_t *nova_bar_create(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data)
{
    nova_bar_t *b = lv_malloc_zeroed(sizeof(*b));
    LV_ASSERT_MALLOC(b);
    b->fsm = fsm;
    b->on_fx = on_fx;
    b->ud = user_data;
    b->opa = 255;
    b->last_act = fsm->activity;

    b->bar = plain(parent);
    lv_obj_set_height(b->bar, BAR_H);
    lv_obj_set_style_radius(b->bar, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b->bar, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b->bar, 18, 0);
    lv_obj_set_style_border_color(b->bar, lv_color_white(), 0);
    lv_obj_set_style_border_opa(b->bar, 23, 0);
    lv_obj_set_style_border_width(b->bar, 1, 0);
    lv_obj_set_flex_flow(b->bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b->bar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
#if CB_RENDER_GPU
    lv_obj_set_style_blur_backdrop(b->bar, true, 0);   /* glass on the GPU only (spec section 7) */
    lv_obj_set_style_blur_radius(b->bar, 24, 0);
#endif

    b->orb = orb_create(b->bar, ORB_D);
    lv_obj_add_event_cb(b->orb, orb_click, LV_EVENT_CLICKED, b);

    b->text_col = plain(b->bar);
    lv_obj_set_flex_grow(b->text_col, 1);
    lv_obj_set_height(b->text_col, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(b->text_col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(b->text_col, 2, 0);
    b->state_lbl = lv_label_create(b->text_col);
    lv_obj_set_style_text_font(b->state_lbl, cb_font(CB_F_SMALL), 0);
    lv_obj_set_style_text_letter_space(b->state_lbl, 2, 0);
    b->text_lbl = lv_label_create(b->text_col);
    /* DOTS only truncates at a fixed height; content height would let a long transcript wrap out of the bar. */
    lv_obj_set_size(b->text_lbl, lv_pct(100), lv_font_get_line_height(cb_font(CB_F_BODY)));
    lv_label_set_long_mode(b->text_lbl, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_font(b->text_lbl, cb_font(CB_F_BODY), 0);

    b->ctrls = plain(b->bar);
    lv_obj_set_size(b->ctrls, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_align(b->ctrls, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(b->ctrls, 4, 0);
    lv_obj_set_style_pad_column(b->ctrls, 14, 0);
    b->mic = round_button(b->ctrls, 36, lv_color_white(), 20);
    mic_glyph(b->mic);
    b->slash = lv_line_create(b->mic);
    lv_obj_set_style_line_width(b->slash, 2, 0);
    lv_obj_set_style_line_color(b->slash, cb_col(CB_RED), 0);
    lv_obj_set_style_line_rounded(b->slash, true, 0);
    lv_obj_set_clickable(b->slash, false);
    lv_obj_add_event_cb(b->mic, mic_click, LV_EVENT_CLICKED, b);
    b->end = round_button(b->ctrls, 36, cb_col(CB_RED), 230);
    glyph_label(b->end, "\xC3\x97", CB_TEXT_HI);
    lv_obj_add_event_cb(b->end, end_click, LV_EVENT_CLICKED, b);

    b->tuck = round_button(b->bar, 36, lv_color_white(), 20);
    glyph_label(b->tuck, "\xE2\x88\x92", CB_TEXT_2);
    lv_obj_add_event_cb(b->tuck, tuck_click, LV_EVENT_CLICKED, b);

    apply_layout(b, fsm->view);
    set_width(b, target_width(fsm->view));
    b->target = fsm->view;
    refresh_state(b);
    lv_obj_add_event_cb(b->bar, bar_delete, LV_EVENT_DELETE, b);
    return b;
}

lv_obj_t *nova_bar_obj(nova_bar_t *b) { return b->bar; }

lv_obj_t *nova_bar_orb(nova_bar_t *b) { return b->orb; }

bool nova_bar_is_animating(const nova_bar_t *b) { return b->phase != PH_NONE; }

void nova_bar_set_reduce_motion(nova_bar_t *b, bool on) { b->reduce = on; }

lv_obj_t *nova_bar_text_col(nova_bar_t *b) { return b->text_col; }

float nova_bar_spring(float t_s, bool expand) { return spring(t_s, expand); }

lv_obj_t *nova_bar_round_button(lv_obj_t *parent, int32_t size, lv_color_t bg, lv_opa_t opa)
{
    return round_button(parent, size, bg, opa);
}

void nova_bar_mic_glyph(lv_obj_t *btn) { mic_glyph(btn); }

lv_obj_t *nova_bar_glyph(lv_obj_t *btn, const char *txt, cb_color_t c) { return glyph_label(btn, txt, c); }
