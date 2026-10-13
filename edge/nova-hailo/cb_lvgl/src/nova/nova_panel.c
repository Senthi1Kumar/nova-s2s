#include "nova/nova_panel.h"
#include "cb_ambient.h"
#include "cb_theme.h"
#include "nova/avatar.h"
#include "widgets/ui_kit.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

#define FACE 84
#define PAD 18
#define BTN 40
#define FADE_OUT_MS 110   /* the bar's morph timings, so both feel the same */
#define SPRING_MS 600
#define FADE_IN_MS 240
#define FADE_IN_AT 45
#define REPLY_LINES 3
#define MARQUEE_PX_S 55

#define TXT_IDLE "Say \xE2\x80\x9CHey Nova\xE2\x80\x9D, or tap me"
#define TXT_MUTED "Mic off \xC2\xB7 tap the mic to unmute"
#define TXT_LISTEN "Listening\xE2\x80\xA6"
#define TXT_THINK "Thinking\xE2\x80\xA6"
#define TXT_GREET "Hi, I\xE2\x80\x99m Nova \xC2\xB7 how can I help?"

enum { PH_NONE, PH_FADE_OUT, PH_HEIGHT };

struct nova_panel {
    lv_obj_t *box, *av, *dot, *text_col, *state_lbl, *line_lbl, *ctrls, *mic, *slash, *end, *tuck, *reply, *table;
    nova_fsm_t *fsm;
    nova_bar_fx_cb_t on_fx;
    void *ud;
    nv_view_t shown, target;
    nv_activity_t last_act;
    int phase;
    int32_t opa, h, h_from, h_to;
    bool boosted, reduce, has_user, has_reply, has_card, notified;
    int32_t max_h;   /* the panel's height budget when open; 0 = none */
    lv_point_precise_t slash_pts[2];
};

/* ---------- height morph (the bar's, on the vertical axis) ---------- */

static void hold_boost(nova_panel_t *p, bool on)
{
    if(on == p->boosted) return;
    p->boosted = on;
    cb_frame_boost(on);
}

static void set_notified(nova_panel_t *p, bool on)
{
    p->notified = on;
    lv_obj_set_hidden(p->dot, !on);
}

static void apply_layout(nova_panel_t *p, nv_view_t v)
{
    bool rest = v == NV_VIEW_REST;
    lv_obj_set_hidden(p->reply, rest || !p->has_reply);
    lv_obj_set_hidden(p->table, rest || !p->has_card);
    lv_obj_set_hidden(p->tuck, rest);
    if(!rest) set_notified(p, false);   /* opening reads the notification */
    p->shown = v;
}

/* Content height of the current layout, measured without showing it. */
static int32_t measure(nova_panel_t *p);

/* The open layout's height whatever is shown, so a card can be cut to the budget while tucked. */
static int32_t open_height(nova_panel_t *p)
{
    lv_obj_set_hidden(p->reply, !p->has_reply);
    lv_obj_set_hidden(p->table, false);
    lv_obj_set_hidden(p->tuck, false);
    int32_t h = measure(p);
    apply_layout(p, p->shown);
    return h;
}

static int32_t measure(nova_panel_t *p)
{
    lv_obj_set_height(p->box, LV_SIZE_CONTENT);
    lv_obj_update_layout(p->box);
    int32_t h = lv_obj_get_height(p->box);
    lv_obj_set_height(p->box, p->h);
    return h;
}

static void set_height(nova_panel_t *p, int32_t h)
{
    p->h = h;
    lv_obj_set_height(p->box, h);
}

static void height_exec(void *var, int32_t v)
{
    nova_panel_t *p = var;
    float x = v >= 1000 ? 1.0f : nova_bar_spring((float)v * SPRING_MS / 1.0e6f, p->h_to > p->h_from);
    set_height(p, p->h_from + (int32_t)lroundf((float)(p->h_to - p->h_from) * x));
}

static void fade_exec(void *var, int32_t v)
{
    nova_panel_t *p = var;
    p->opa = v;
    lv_obj_t *parts[4] = { p->text_col, p->ctrls, p->reply, p->table };
    for(int i = 0; i < 4; i++) lv_obj_set_style_opa(parts[i], (lv_opa_t)v, 0);
    lv_obj_set_style_translate_x(p->text_col, -10 * (255 - v) / 255, 0);
}

static void run(nova_panel_t *p, lv_anim_exec_xcb_t exec, int32_t from, int32_t to, uint32_t ms,
                lv_anim_path_cb_t path, lv_anim_completed_cb_t done, uint32_t delay)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, p);
    lv_anim_set_exec_cb(&a, exec);
    lv_anim_set_values(&a, from, to);
    lv_anim_set_duration(&a, ms);
    lv_anim_set_delay(&a, delay);
    lv_anim_set_path_cb(&a, path);
    lv_anim_set_user_data(&a, p);
    lv_anim_set_completed_cb(&a, done);
    lv_anim_start(&a);
}

static void settle(nova_panel_t *p)
{
    if(lv_anim_get(p, height_exec) || lv_anim_get(p, fade_exec)) return;
    p->phase = PH_NONE;
    hold_boost(p, false);
}

static void anim_done(lv_anim_t *a) { settle(lv_anim_get_user_data(a)); }

static void height_done(lv_anim_t *a)
{
    nova_panel_t *p = lv_anim_get_user_data(a);
    set_height(p, p->h_to);
    settle(p);
}

static void spring_to(nova_panel_t *p, int32_t h)
{
    lv_anim_delete(p, height_exec);
    p->h_from = p->h;
    p->h_to = h;
    if(p->h_from != h) run(p, height_exec, 0, 1000, SPRING_MS, lv_anim_path_linear, height_done, 0);
}

static void start_height(nova_panel_t *p)
{
    apply_layout(p, p->target);
    p->phase = PH_HEIGHT;
    spring_to(p, measure(p));
    bool moving = p->h_from != p->h_to;
    run(p, fade_exec, p->opa, 255, FADE_IN_MS, lv_anim_path_ease_out, anim_done,
        moving ? SPRING_MS * FADE_IN_AT / 100 : 0);
}

static void fade_out_done(lv_anim_t *a) { start_height(lv_anim_get_user_data(a)); }

static void request_view(nova_panel_t *p, nv_view_t v)
{
    if(v == p->target && p->phase != PH_NONE) return;
    if(v == p->shown && p->phase == PH_NONE && p->h == measure(p)) return;
    lv_anim_delete(p, fade_exec);
    lv_anim_delete(p, height_exec);
    p->target = v;
    if(p->reduce) {
        apply_layout(p, v);
        set_height(p, measure(p));
        fade_exec(p, 255);
        p->phase = PH_NONE;
        return;
    }
    p->phase = PH_FADE_OUT;
    hold_boost(p, true);
    uint32_t ms = FADE_OUT_MS * (uint32_t)p->opa / 255;
    if(ms == 0) start_height(p);
    else run(p, fade_exec, p->opa, 0, ms, lv_anim_path_ease_in, fade_out_done, 0);
}

/* Content changed (a reply appeared, a card arrived): spring to the new height without the fade. */
static void relayout(nova_panel_t *p)
{
    if(p->phase == PH_FADE_OUT) return;   /* start_height measures after the swap */
    apply_layout(p, p->shown);
    int32_t h = measure(p);
    if(h == p->h && p->phase == PH_NONE) return;
    if(p->reduce) {
        set_height(p, h);
        return;
    }
    hold_boost(p, true);
    p->phase = PH_HEIGHT;
    spring_to(p, h);
    settle(p);   /* no-op while the spring or a fade runs */
}

/* ---------- state ---------- */

static cb_color_t state_color(const nova_fsm_t *f)
{
    if(f->muted) return CB_RED;
    switch(f->activity) {
        case NV_LISTENING: return CB_CYAN;
        case NV_THINKING: return CB_VIOLET;
        case NV_SPEAKING: return CB_GREEN;
        default: return CB_TEXT_LO;
    }
}

static void set_line(nova_panel_t *p, const char *text, cb_color_t c)
{
    lv_label_set_text(p->line_lbl, text);
    lv_obj_set_style_text_color(p->line_lbl, cb_col(c), 0);
}

static void refresh_state(nova_panel_t *p)
{
    const nova_fsm_t *f = p->fsm;
    if(p->last_act == NV_IDLE && f->activity == NV_LISTENING) {   /* a new turn starts clean */
        p->has_user = p->has_reply = p->has_card = false;
        lv_label_set_text(p->reply, "");
        lv_obj_clean(p->table);
        relayout(p);
    }
    p->last_act = f->activity;
    lv_label_set_text_fmt(p->state_lbl, "NOVA \xC2\xB7 %s", f->muted ? "MUTED" : nova_activity_name(f->activity));
    lv_obj_set_style_text_color(p->state_lbl, cb_col(state_color(f)), 0);
    avatar_set_state(p->av, f->activity, f->muted);

    lv_obj_set_hidden(p->slash, !f->muted);
    lv_obj_set_style_bg_color(p->mic, f->muted ? cb_col(CB_RED) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(p->mic, f->muted ? 56 : 20, 0);
    lv_obj_set_style_border_width(p->mic, f->muted ? 1 : 0, 0);
    lv_obj_set_style_border_color(p->mic, cb_col(CB_RED), 0);

    if(f->muted) set_line(p, TXT_MUTED, CB_TEXT_MID);
    else if(p->has_user) return;
    else if(f->activity == NV_LISTENING) set_line(p, TXT_LISTEN, CB_TEXT_MID);
    else if(f->activity == NV_THINKING) set_line(p, TXT_THINK, CB_TEXT_MID);
    else if(f->activity == NV_IDLE) set_line(p, TXT_IDLE, CB_TEXT_MID);
}

void nova_panel_apply(nova_panel_t *p, unsigned fx)
{
    if(fx & (NV_FX_ACTIVITY | NV_FX_MUTE)) refresh_state(p);
    if(fx & NV_FX_VIEW) request_view(p, p->fsm->view);
}

/* ---------- text and cards ---------- */

void nova_panel_set_text(nova_panel_t *p, const char *text, bool user)
{
    if(!user) {
        nova_panel_stream_text(p, text);
        return;
    }
    char q[400];
    snprintf(q, sizeof q, "\xE2\x80\x9C%s\xE2\x80\x9D", text);
    p->has_user = true;
    set_line(p, q, CB_TEXT_HI);
}

void nova_panel_stream_text(nova_panel_t *p, const char *text)
{
    bool had = p->has_reply;
    p->has_reply = text && *text;
    lv_label_set_text(p->reply, text ? text : "");
    if(had != p->has_reply) relayout(p);
}

static lv_obj_t *small_caps(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = ui_label(parent, text, CB_F_SMALL, CB_TEXT_LO);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    return l;
}

void nova_panel_set_card(nova_panel_t *p, const nova_card_view_t *c)
{
    lv_obj_clean(p->table);
    if(c->kind && *c->kind) small_caps(p->table, c->kind);
    ui_label_line(p->table, c->title && *c->title ? c->title : "Nova", CB_F_VALUE, CB_TEXT_HI);
    for(int i = 0; i < c->n_lines && i < 2; i++) {
        lv_obj_t *l = ui_label(p->table, c->lines[i], CB_F_BODY, CB_TEXT_2);
        lv_obj_set_width(l, lv_pct(100));
        lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    }
    lv_obj_t *rows[NOVA_CARD_ROWS];
    int n = 0;
    for(int i = 0; i < c->n_rows && i < NOVA_CARD_ROWS; i++) {
        lv_obj_t *row = rows[n++] = ui_row(p->table, 10);
        lv_obj_set_style_pad_hor(row, 12, 0);
        lv_obj_set_style_pad_ver(row, 8, 0);
        lv_obj_set_style_radius(row, 10, 0);
        lv_obj_set_style_bg_color(row, lv_color_white(), 0);
        lv_obj_set_style_bg_opa(row, i % 2 ? 6 : 14, 0);   /* zebra rows read as a table */
        ui_dot(row, CB_CYAN, false);
        lv_obj_t *t = ui_label_line(row, c->rows[i].title ? c->rows[i].title : "", CB_F_BODY, CB_TEXT_HI);
        lv_obj_set_width(t, 0);
        lv_obj_set_flex_grow(t, 1);
        if(c->rows[i].sub && *c->rows[i].sub) ui_label(row, c->rows[i].sub, CB_F_LABEL, CB_TEXT_MID);
    }
    int more = c->more;
    lv_obj_t *m = ui_label(p->table, "", CB_F_LABEL, CB_TEXT_LO);
    lv_label_set_text_fmt(m, "+%d more", more);
    lv_obj_set_hidden(m, more <= 0);
    p->has_card = true;
    while(p->max_h && n > 0 && open_height(p) > p->max_h) {   /* fold the rows that do not fit */
        lv_obj_delete(rows[--n]);
        lv_label_set_text_fmt(m, "+%d more", ++more);
        lv_obj_set_hidden(m, false);
    }
    if(p->shown == NV_VIEW_REST) set_notified(p, true);   /* tucked: a notification */
    relayout(p);
}

void nova_panel_set_pending(nova_panel_t *p, bool pending)
{
    if(!p) return;
    lv_obj_set_style_opa(p->mic, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
    lv_obj_set_style_opa(p->end, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
}

void nova_panel_greet(nova_panel_t *p)
{
    avatar_greet(p->av);
    if(!p->has_user && !p->fsm->muted) set_line(p, TXT_GREET, CB_TEXT_HI);
}

/* ---------- taps ---------- */

static void handle(nova_panel_t *p, nv_event_t ev)
{
    unsigned fx = nova_fsm_handle(p->fsm, ev, NULL, lv_tick_get());
    nova_panel_apply(p, fx);
    if(p->on_fx) p->on_fx(fx, p->ud);
}

static void avatar_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_ORB); }
static void mic_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_MIC); }
static void end_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_END); }
static void tuck_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_TUCK); }

static void panel_delete(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    lv_anim_delete(p, NULL);
    hold_boost(p, false);
    lv_free(p);
}

/* ---------- construction ---------- */

/* The avatar's halo, antenna and hops, and the dot, draw past their row: an overflow-visible parent still clips
   its children to its own ext draw size, which is 0 unless asked. */
static void ext_draw_cb(lv_event_t *e) { lv_event_set_ext_draw_size(e, (int32_t)(0.2f * FACE) + 4); }

static void overflow(lv_obj_t *o)
{
    lv_obj_set_overflow_visible(o, true);
    lv_obj_add_event_cb(o, ext_draw_cb, LV_EVENT_REFR_EXT_DRAW_SIZE, NULL);
    lv_obj_refresh_ext_draw_size(o);
}

nova_panel_t *nova_panel_create(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data)
{
    nova_panel_t *p = lv_malloc_zeroed(sizeof(*p));
    LV_ASSERT_MALLOC(p);
    p->fsm = fsm;
    p->on_fx = on_fx;
    p->ud = user_data;
    p->opa = 255;
    p->last_act = fsm->activity;

    p->box = ui_plain(parent);
    lv_obj_set_width(p->box, lv_pct(100));
    lv_obj_set_style_bg_color(p->box, lv_color_hex(0x0e1117), 0);
    lv_obj_set_style_bg_opa(p->box, 225, 0);
    lv_obj_set_style_border_color(p->box, lv_color_white(), 0);
    lv_obj_set_style_border_opa(p->box, 23, 0);
    lv_obj_set_style_border_width(p->box, 1, 0);
    lv_obj_set_style_radius(p->box, 26, 0);
    lv_obj_set_style_pad_all(p->box, PAD, 0);
    lv_obj_set_flex_flow(p->box, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(p->box, 14, 0);
#if CB_RENDER_GPU
    lv_obj_set_style_blur_backdrop(p->box, true, 0);
    lv_obj_set_style_blur_radius(p->box, 24, 0);
#endif

    lv_obj_t *top = ui_row(p->box, 16);
    overflow(top);
    lv_obj_t *wrap = ui_plain(top);
    lv_obj_set_size(wrap, FACE, FACE);
    overflow(wrap);
    p->av = avatar_create(wrap, FACE);
    lv_obj_center(p->av);
    lv_obj_add_event_cb(p->av, avatar_click, LV_EVENT_CLICKED, p);
    p->dot = ui_plain(wrap);
    lv_obj_set_size(p->dot, 18, 18);
    lv_obj_set_style_radius(p->dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(p->dot, cb_col(CB_GREEN), 0);
    lv_obj_set_style_bg_opa(p->dot, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(p->dot, lv_color_hex(0x0e1117), 0);
    lv_obj_set_style_border_width(p->dot, 3, 0);
    lv_obj_align(p->dot, LV_ALIGN_TOP_RIGHT, 6, -2);
    set_notified(p, false);

    p->text_col = ui_col(top, 6);
    lv_obj_set_width(p->text_col, 0);
    lv_obj_set_flex_grow(p->text_col, 1);
    p->state_lbl = small_caps(p->text_col, "NOVA");
    p->line_lbl = lv_label_create(p->text_col);
    lv_obj_set_width(p->line_lbl, lv_pct(100));
    lv_obj_set_style_text_font(p->line_lbl, cb_font(CB_F_VALUE), 0);
    lv_label_set_long_mode(p->line_lbl, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);   /* a long question slides by */
    lv_obj_set_style_anim_duration(p->line_lbl, lv_anim_speed_clamped(MARQUEE_PX_S, 300, 10000), 0);

    p->ctrls = ui_row(top, 10);
    lv_obj_set_width(p->ctrls, LV_SIZE_CONTENT);
    p->mic = nova_bar_round_button(p->ctrls, BTN, lv_color_white(), 20);
    nova_bar_mic_glyph(p->mic);
    p->slash = lv_line_create(p->mic);
    p->slash_pts[0] = (lv_point_precise_t){ BTN / 4, BTN / 4 };
    p->slash_pts[1] = (lv_point_precise_t){ BTN - BTN / 4, BTN - BTN / 4 };
    lv_line_set_points(p->slash, p->slash_pts, 2);
    lv_obj_set_size(p->slash, BTN, BTN);
    lv_obj_set_style_line_width(p->slash, 2, 0);
    lv_obj_set_style_line_color(p->slash, cb_col(CB_RED), 0);
    lv_obj_set_style_line_rounded(p->slash, true, 0);
    lv_obj_set_clickable(p->slash, false);
    lv_obj_add_event_cb(p->mic, mic_click, LV_EVENT_CLICKED, p);
    p->end = nova_bar_round_button(p->ctrls, BTN, cb_col(CB_RED), 230);
    nova_bar_glyph(p->end, "\xC3\x97", CB_TEXT_HI);
    lv_obj_add_event_cb(p->end, end_click, LV_EVENT_CLICKED, p);
    p->tuck = nova_bar_round_button(p->ctrls, BTN, lv_color_white(), 20);
    nova_bar_glyph(p->tuck, "\xE2\x88\x92", CB_TEXT_2);
    lv_obj_add_event_cb(p->tuck, tuck_click, LV_EVENT_CLICKED, p);

    p->reply = lv_label_create(p->box);
    lv_obj_set_size(p->reply, lv_pct(100), REPLY_LINES * lv_font_get_line_height(cb_font(CB_F_BODY)));
    lv_label_set_long_mode(p->reply, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_font(p->reply, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_text_color(p->reply, cb_col(CB_TEXT_HI), 0);
    lv_label_set_text(p->reply, "");
    p->table = ui_col(p->box, 6);

    p->target = fsm->view;
    apply_layout(p, fsm->view);
    refresh_state(p);
    set_height(p, measure(p));
    lv_obj_add_event_cb(p->box, panel_delete, LV_EVENT_DELETE, p);
    return p;
}

void nova_panel_set_reduce_motion(nova_panel_t *p, bool on) { p->reduce = on; }

void nova_panel_set_max_height(nova_panel_t *p, int32_t h) { p->max_h = h; }

lv_obj_t *nova_panel_obj(nova_panel_t *p) { return p->box; }

lv_obj_t *nova_panel_avatar(nova_panel_t *p) { return p->av; }

lv_obj_t *nova_panel_line(nova_panel_t *p) { return p->line_lbl; }

bool nova_panel_is_animating(const nova_panel_t *p) { return p->phase != PH_NONE; }

bool nova_panel_notified(const nova_panel_t *p) { return p->notified; }
