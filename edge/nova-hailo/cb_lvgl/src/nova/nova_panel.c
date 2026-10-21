#include "nova/nova_panel.h"
#include "cb_ambient.h"
#include "cb_theme.h"
#include "nova/avatar.h"
#include "widgets/ui_kit.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#define FACE 84   /* the room the face takes in the row */
#define AVATAR_BOX 150   /* its drawing box: the head is 0.54 of it, the rest is hop and sparkle room */
#define PAD 18
#define BTN 40
#define FADE_OUT_MS 110   /* the bar's morph timings, so both feel the same */
#define SPRING_MS 600
#define FADE_IN_MS 240
#define FADE_IN_AT 45
#define REPLY_LINES 3
#define DOCK_REPLY_LINES 4
#define DOCK_ROW_H 52
#define DOCK_BTN 52
#define MQ_HOLD_MS 1000
#define MQ_PX_S 45
#define GUIDE_HOLD_MS 4000   /* a spoken route prompt keeps the line this long against banner updates */
#define BUBBLE_LINES 2
#define BUBBLE_PAD_H 16
#define DOCK_FACE 80
#define DOCK_AVATAR_BOX 132   /* its top stands 48 px above the bar */
#define DOCK_AV_DY (-26)
#define DOCK_BOTTOM 8
#define DOCK_PAD_H 14
#define DOCK_GAP 16   /* between the blocks of the answer */
#define DOCK_RISE 20   /* extra room above the row, where the avatar stands */
#define DOCK_OPA 200
#define DOCK_BLUR_OPA 140
#define DOCK_ICON 22
#define TINT_MS 400
#define MARQUEE_PX_S 55

#define TXT_IDLE "Say \xE2\x80\x9CHey Nova\xE2\x80\x9D, or tap me"
#define TXT_MUTED "Mic off \xC2\xB7 tap the mic to unmute"
#define TXT_LISTEN "Listening\xE2\x80\xA6"
#define TXT_THINK "Thinking\xE2\x80\xA6"
#define TXT_NAV "NOVA \xC2\xB7 NAVIGATING"
#define TXT_GREET "Hi, I\xE2\x80\x99m Nova \xC2\xB7 how can I help?"

#if CB_RENDER_GPU
static int s_dock_blur = 22;   /* backdrop blur radius; --dock-blur changes it, 0 = off */
#else
static int s_dock_blur;        /* the software renderer has no backdrop blur */
#endif

enum { PH_NONE, PH_FADE_OUT, PH_HEIGHT };

struct nova_panel {
    lv_obj_t *box, *top, *wash, *hilite, *tuck_v, *av, *dot, *text_col, *state_lbl, *line_lbl, *ctrls, *mic, *slash, *end, *tuck, *reply, *table;
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
    bool dock;
    lv_obj_t *line_clip, *bubble_row, *bubble_lbl;
    int32_t mq_over;   /* how far the transcript overruns its box */
    lv_timer_t *tuck_timer;
    lv_color_t tint_from, tint_to;
    bool drop_quote;   /* the route ended mid-turn: the quote goes when the bar tucks */
    lv_grad_dsc_t wash_grad;
    lv_color_t wash_col;
    lv_opa_t wash_opa;
    char route[200];   /* dock: the current instruction while a route runs, "" otherwise */
    uint32_t guide_until;   /* tick before which banner updates leave a spoken prompt alone; 0 = none */
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
    if(p->dock) on = false;
    p->notified = on;
    lv_obj_set_hidden(p->dot, !on);
}

static void apply_layout(nova_panel_t *p, nv_view_t v)
{
    bool rest = v == NV_VIEW_REST;
    lv_obj_set_hidden(p->reply, rest || !p->has_reply);
    lv_obj_set_hidden(p->table, rest || !p->has_card);
    bool more = p->has_card || p->has_reply;
    lv_obj_set_hidden(p->tuck, rest && !(p->dock && more));   /* dock: tucked, the button opens it again */
    if(p->tuck_v) lv_obj_set_hidden(p->tuck_v, !rest);
    if(p->dock) lv_obj_set_style_margin_top(p->top, !rest && (more || p->has_user) ? DOCK_RISE : 0, 0);
    if(p->bubble_row) lv_obj_set_hidden(p->bubble_row, rest || !p->has_user);
    if(!rest || p->dock) set_notified(p, false);   /* opening reads the notification */
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
    if(p->bubble_row) lv_obj_set_hidden(p->bubble_row, !p->has_user);
    if(p->dock) lv_obj_set_style_margin_top(p->top, DOCK_RISE, 0);
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
    lv_obj_t *parts[5] = { p->text_col, p->ctrls, p->reply, p->table, p->bubble_row };
    for(int i = 0; i < 5; i++)
        if(parts[i]) lv_obj_set_style_opa(parts[i], (lv_opa_t)v, 0);
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

/* ---------- dock mode ---------- */

static void cancel_tuck(nova_panel_t *p)
{
    if(p->tuck_timer) lv_timer_pause(p->tuck_timer);
}

static void refresh_line(nova_panel_t *p);

static void dock_open(nova_panel_t *p, bool open)
{
    request_view(p, open ? NV_VIEW_OPEN : NV_VIEW_REST);
    refresh_line(p);   /* the route line shows while tucked, the turn's words while open */
}

/* Tucks through the FSM so its view and the panel agree. */
static void tuck_via_fsm(nova_panel_t *p)
{
    nova_panel_apply(p, nova_fsm_handle(p->fsm, NV_EV_TAP_TUCK, NULL, lv_tick_get()));
    dock_open(p, false);
}

static bool in_turn(const nova_panel_t *p)
{
    const nova_fsm_t *f = p->fsm;
    return f->activity != NV_IDLE || f->user_speaking;
}

static void tuck_cb(lv_timer_t *t)
{
    nova_panel_t *p = lv_timer_get_user_data(t);
    lv_timer_pause(t);
    if(!p->has_card && !in_turn(p)) tuck_via_fsm(p);   /* re-armed when the turn really ends */
}

static void arm_tuck(nova_panel_t *p)
{
    if(!p->tuck_timer) {
        p->tuck_timer = lv_timer_create(tuck_cb, NOVA_PANEL_TUCK_MS, p);
        lv_timer_set_repeat_count(p->tuck_timer, -1);
    }
    lv_timer_set_period(p->tuck_timer, NOVA_PANEL_TUCK_MS);
    lv_timer_reset(p->tuck_timer);
    lv_timer_resume(p->tuck_timer);
}

static void tint_exec(void *var, int32_t v)
{
    nova_panel_t *p = var;
    lv_obj_set_style_border_color(p->box, lv_color_mix(p->tint_to, p->tint_from, (lv_opa_t)v), 0);
}

static void set_tint(nova_panel_t *p, lv_color_t to)
{
    lv_color_t now = lv_obj_get_style_border_color(p->box, 0);
    lv_anim_delete(p, tint_exec);
    p->tint_from = now;
    p->tint_to = to;
    if(p->reduce) {
        tint_exec(p, 255);
        return;
    }
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, p);
    lv_anim_set_exec_cb(&a, tint_exec);
    lv_anim_set_values(&a, 0, 255);
    lv_anim_set_duration(&a, TINT_MS);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_start(&a);
}

#define GLASS_EDGE 0x3a3e48

/* The top-left wash takes the state's colour; it is restyled only when that colour changes. */
static void wash_set(nova_panel_t *p, lv_color_t col, lv_opa_t opa)
{
    if(lv_color_eq(col, p->wash_col) && opa == p->wash_opa) return;
    p->wash_col = col;
    p->wash_opa = opa;
    lv_color_t cols[2] = { col, col };
    lv_opa_t opas[2] = { opa, 0 };
    lv_grad_init_stops(&p->wash_grad, cols, opas, NULL, 2);
    lv_grad_radial_init(&p->wash_grad, 0, 0, lv_pct(55), lv_pct(110), LV_GRAD_EXTEND_PAD);
    lv_obj_set_style_bg_grad(p->wash, &p->wash_grad, 0);
    lv_obj_invalidate(p->wash);
}

static void box_size_cb(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    lv_obj_t *b = p->box;
    int32_t w = lv_obj_get_width(b), h = lv_obj_get_height(b);
    int32_t r = lv_obj_get_style_radius(b, 0), px = lv_obj_get_style_pad_left(b, 0) + 1, py = lv_obj_get_style_pad_top(b, 0) + 1;
    lv_obj_set_size(p->wash, w - 2, h - 2);
    lv_obj_set_pos(p->wash, -px + 1, -py + 1);
    lv_obj_set_size(p->hilite, w - 2 * r, 1);
    lv_obj_set_pos(p->hilite, r - px, -py + 1);
}

static lv_color_t glass_tint(bool user)
{
    return lv_color_mix(cb_col(user ? CB_CYAN : CB_GREEN), lv_color_hex(GLASS_EDGE), 200);
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

/* Dock marquee: hold, slide to the end of the text, hold, again. One anim whose value is the time in the cycle. */
static void mq_exec(void *var, int32_t t)
{
    nova_panel_t *p = var;
    int32_t slid = (t - MQ_HOLD_MS) * MQ_PX_S / 1000;
    if(slid < 0) slid = 0;
    if(slid > p->mq_over) slid = p->mq_over;
    lv_obj_set_x(p->line_lbl, -slid);
}

static void marquee_restart(nova_panel_t *p)
{
    lv_anim_delete(p, mq_exec);
    lv_obj_set_x(p->line_lbl, 0);
    lv_obj_update_layout(p->line_clip);
    p->mq_over = lv_obj_get_width(p->line_lbl) - lv_obj_get_width(p->line_clip);
    if(p->mq_over <= 0 || p->reduce) return;
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, p);
    lv_anim_set_exec_cb(&a, mq_exec);
    lv_anim_set_values(&a, 0, 2 * MQ_HOLD_MS + p->mq_over * 1000 / MQ_PX_S);
    lv_anim_set_duration(&a, (uint32_t)(2 * MQ_HOLD_MS + p->mq_over * 1000 / MQ_PX_S));
    lv_anim_set_path_cb(&a, lv_anim_path_linear);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);
}

/* The instruction part of a route line ("In 300 m · Turn left" -> "Turn left"). */
static const char *route_instruction(const char *line)
{
    const char *sep = strstr(line, " \xC2\xB7 ");
    return sep ? sep + 4 : line;
}

static void set_line(nova_panel_t *p, const char *text, cb_color_t c)
{
    lv_label_set_text(p->line_lbl, text);
    lv_obj_set_style_text_color(p->line_lbl, cb_col(c), 0);
    if(p->dock) marquee_restart(p);
}

/* The "you" bubble: the user's words, at most BUBBLE_LINES lines inside 88 % of the width. */
static void set_bubble(nova_panel_t *p, const char *text)
{
    lv_obj_t *l = p->bubble_lbl;
    const lv_font_t *f = lv_obj_get_style_text_font(l, 0);
    lv_obj_update_layout(p->box);
    int32_t maxw = lv_obj_get_content_width(p->box) * 88 / 100 - 2 * BUBBLE_PAD_H;
    int32_t lh = lv_font_get_line_height(f);
    lv_point_t one, wrapped;
    lv_text_get_size(&one, text, f, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    int32_t w = one.x < maxw ? one.x + 2 : maxw;
    lv_text_get_size(&wrapped, text, f, 0, 0, w, LV_TEXT_FLAG_NONE);
    lv_label_set_text(l, text);
    lv_obj_set_size(l, w, wrapped.y < BUBBLE_LINES * lh ? wrapped.y : BUBBLE_LINES * lh);
}

/* The route's instruction owns the line while no turn is on: tucked, or open with nothing the driver said. */
static bool route_shown(const nova_panel_t *p)
{
    return p->dock && p->route[0] && !p->fsm->muted && !in_turn(p) && (p->target == NV_VIEW_REST || !p->has_user);
}

/* The state label and the one-line text under it. */
static void refresh_line(nova_panel_t *p)
{
    const nova_fsm_t *f = p->fsm;
    if(p->drop_quote && p->target == NV_VIEW_REST && !in_turn(p)) p->has_user = p->drop_quote = false;
    if(route_shown(p)) {
        lv_label_set_text(p->state_lbl, TXT_NAV);
        lv_obj_set_style_text_color(p->state_lbl, cb_col(CB_TEXT_LO), 0);
        const char *shown = lv_label_get_text(p->line_lbl);
        if(strcmp(shown, p->route)) {
            if(!strcmp(route_instruction(shown), route_instruction(p->route))) {   /* only the distance moved: keep the slide */
                lv_label_set_text(p->line_lbl, p->route);
                lv_obj_update_layout(p->line_clip);
                p->mq_over = lv_obj_get_width(p->line_lbl) - lv_obj_get_width(p->line_clip);
            }
            else set_line(p, p->route, CB_TEXT_HI);
        }
        return;
    }
    const char *name = f->muted ? "MUTED" : nova_activity_name(f->activity);
    if(p->dock && !f->muted && f->activity == NV_IDLE && (p->has_reply || p->has_card)) name = "ANSWERED";
    lv_label_set_text_fmt(p->state_lbl, "NOVA \xC2\xB7 %s", name);
    lv_obj_set_style_text_color(p->state_lbl, cb_col(state_color(f)), 0);
    if(f->muted) set_line(p, TXT_MUTED, CB_TEXT_MID);
    else if(p->has_user) return;
    else if(f->activity == NV_LISTENING) set_line(p, TXT_LISTEN, CB_TEXT_MID);
    else if(f->activity == NV_THINKING) set_line(p, TXT_THINK, CB_TEXT_MID);
    else if(f->activity == NV_IDLE) set_line(p, TXT_IDLE, CB_TEXT_MID);
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
    if(p->dock) {
        bool ended = (p->last_act == NV_SPEAKING && f->activity != NV_SPEAKING) ||
                     (p->last_act != NV_IDLE && f->activity == NV_IDLE);
        if(ended && !p->has_card) arm_tuck(p);
        if(f->activity == NV_LISTENING || f->activity == NV_THINKING) cancel_tuck(p);
        if(f->activity == NV_IDLE && p->last_act != NV_IDLE) set_tint(p, lv_color_hex(GLASS_EDGE));
        bool idle = !f->muted && f->activity == NV_IDLE;
        wash_set(p, cb_col(idle ? CB_CYAN : state_color(f)), idle ? 26 : 44);
    }
    p->last_act = f->activity;
    avatar_set_state(p->av, f->activity, f->muted);

    lv_obj_set_hidden(p->slash, !f->muted);
    lv_obj_set_style_bg_color(p->mic, f->muted ? cb_col(CB_RED) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(p->mic, f->muted ? 56 : (p->dock ? 26 : 20), 0);
    lv_obj_set_style_border_width(p->mic, f->muted ? 1 : 0, 0);
    lv_obj_set_style_border_color(p->mic, cb_col(CB_RED), 0);
    refresh_line(p);
}

void nova_panel_apply(nova_panel_t *p, unsigned fx)
{
    if(fx & (NV_FX_ACTIVITY | NV_FX_MUTE)) refresh_state(p);
    if(!(fx & NV_FX_VIEW)) return;
    if(!p->dock) request_view(p, p->fsm->view);
    else if(p->fsm->view == NV_VIEW_OPEN) dock_open(p, true);
    else if(!p->has_card) dock_open(p, false);   /* a card keeps the bar open until it is tucked */
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
    bool had = p->has_user;
    p->has_user = true;
    set_line(p, q, CB_TEXT_HI);
    if(p->dock) {
        set_bubble(p, text);
        set_tint(p, glass_tint(true));
        if(!had) relayout(p);
    }
}

/* The reply is as tall as its text, up to DOCK_REPLY_LINES lines, so no gap opens under it. */
static void fit_reply(nova_panel_t *p, const char *text)
{
    const lv_font_t *f = lv_obj_get_style_text_font(p->reply, 0);
    int32_t ls = lv_obj_get_style_text_line_space(p->reply, 0);
    lv_obj_update_layout(p->box);
    lv_point_t sz;
    lv_text_get_size(&sz, text, f, 0, ls, lv_obj_get_content_width(p->box), LV_TEXT_FLAG_NONE);
    int32_t max = DOCK_REPLY_LINES * lv_font_get_line_height(f) + (DOCK_REPLY_LINES - 1) * ls;
    lv_obj_set_height(p->reply, sz.y < max ? sz.y : max);
}

void nova_panel_stream_text(nova_panel_t *p, const char *text)
{
    bool had = p->has_reply;
    p->has_reply = text && *text;
    if(p->dock && p->has_reply) set_tint(p, glass_tint(false));
    lv_label_set_text(p->reply, text ? text : "");
    if(p->dock && text && *text) fit_reply(p, text);
    if(had != p->has_reply) relayout(p);
}

static lv_obj_t *small_caps(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = ui_label(parent, text, CB_F_SMALL, CB_TEXT_LO);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    return l;
}

/* Dock card header: the title in small letter-spaced caps, the note (the card's first line) in yellow at the right. */
static void dock_card_head(nova_panel_t *p, const nova_card_view_t *c)
{
    char up[96];
    snprintf(up, sizeof up, "%s", c->title && *c->title ? c->title : "Nova");
    for(char *s = up; *s; s++) *s = (char)toupper((unsigned char)*s);
    lv_obj_t *head = ui_row(p->table, 10);
    lv_obj_t *t = ui_label_line(head, up, CB_F_SMALL, CB_TEXT_MID);
    lv_obj_set_width(t, 0);
    lv_obj_set_flex_grow(t, 1);
    lv_obj_set_style_text_letter_space(t, 2, 0);
    if(c->n_lines > 0 && c->lines[0] && *c->lines[0]) ui_label(head, c->lines[0], CB_F_SMALL, CB_YELLOW);
    lv_obj_set_style_pad_bottom(head, 8, 0);
}

/* A table row: title at the left, value at the right, a thin line above all but the first. */
static void dock_card_row(lv_obj_t *row, int i, const char *const *title, const char *const *sub)
{
    lv_obj_set_style_pad_ver(row, 9, 0);
    if(i > 0) {
        lv_obj_set_style_border_width(row, 1, 0);
        lv_obj_set_style_border_side(row, LV_BORDER_SIDE_TOP, 0);
        lv_obj_set_style_border_color(row, lv_color_white(), 0);
        lv_obj_set_style_border_opa(row, 20, 0);
    }
    lv_obj_t *t = ui_label_line(row, *title ? *title : "", CB_F_BODY, CB_TEXT_HI);
    lv_obj_set_width(t, 0);
    lv_obj_set_flex_grow(t, 1);
    if(*sub && **sub) ui_label(row, *sub, CB_F_LABEL, CB_TEXT_MID);
}

void nova_panel_set_card(nova_panel_t *p, const nova_card_view_t *c)
{
    lv_obj_clean(p->table);
    if(p->dock) dock_card_head(p, c);
    else {
        if(c->kind && *c->kind) small_caps(p->table, c->kind);
        ui_label_line(p->table, c->title && *c->title ? c->title : "Nova", CB_F_VALUE, CB_TEXT_HI);
    }
    for(int i = p->dock ? 1 : 0; i < c->n_lines && i < 2; i++) {
        lv_obj_t *l = ui_label(p->table, c->lines[i], CB_F_BODY, CB_TEXT_2);
        lv_obj_set_width(l, lv_pct(100));
        lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    }
    lv_obj_t *rows[NOVA_CARD_ROWS];
    int n = 0;
    for(int i = 0; i < c->n_rows && i < NOVA_CARD_ROWS; i++) {
        lv_obj_t *row = rows[n++] = ui_row(p->table, 10);
        if(p->dock) {
            dock_card_row(row, i, &c->rows[i].title, &c->rows[i].sub);
            continue;
        }
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
    if(p->dock) {
        lv_obj_set_width(m, lv_pct(100));
        lv_obj_set_style_text_align(m, LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_style_pad_top(m, 6, 0);
    }
    p->has_card = true;
    while(p->max_h && n > 0 && open_height(p) > p->max_h) {   /* fold the rows that do not fit */
        lv_obj_delete(rows[--n]);
        lv_label_set_text_fmt(m, "+%d more", ++more);
        lv_obj_set_hidden(m, false);
    }
    if(p->dock) {
        cancel_tuck(p);
        dock_open(p, true);   /* a card opens the bar and holds it */
    }
    else if(p->shown == NV_VIEW_REST) set_notified(p, true);   /* tucked: a notification */
    relayout(p);
}

void nova_panel_set_pending(nova_panel_t *p, bool pending)
{
    if(!p) return;
    lv_obj_set_style_opa(p->mic, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
    lv_obj_set_style_opa(p->end, pending ? LV_OPA_50 : LV_OPA_COVER, 0);
}

void nova_panel_set_route_line(nova_panel_t *p, const char *line)
{
    if(!p || !p->dock) return;
    bool was = p->route[0];
    if(line && *line && was && p->guide_until && (int32_t)(p->guide_until - lv_tick_get()) > 0) return;   /* a prompt is being read */
    p->guide_until = 0;
    snprintf(p->route, sizeof p->route, "%s", line ? line : "");
    p->drop_quote = was && !p->route[0];   /* the quote is stale once the turn is over and the bar is tucked */
    refresh_line(p);
}

void nova_panel_guidance(nova_panel_t *p, const char *text)
{
    if(!p || !p->dock || !p->route[0] || !text || !*text) return;   /* a prompt after the route ended is stale */
    p->guide_until = 0;   /* a newer prompt replaces the one being read */
    nova_panel_set_route_line(p, text);
    p->guide_until = lv_tick_get() + GUIDE_HOLD_MS;
    if(!p->guide_until) p->guide_until = 1;
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

static void avatar_click(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    handle(p, NV_EV_TAP_ORB);
    if(p->dock && (p->has_card || p->has_reply)) dock_open(p, true);
}
static void mic_click(lv_event_t *e) { handle(lv_event_get_user_data(e), NV_EV_TAP_MIC); }
static void end_click(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    handle(p, NV_EV_TAP_END);
    nova_panel_tuck(p);
}
static void tuck_click(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    if(p->dock && p->target == NV_VIEW_REST) {   /* the plus: show the answer again */
        dock_open(p, true);
        if(!p->has_card) arm_tuck(p);
        return;
    }
    handle(p, NV_EV_TAP_TUCK);
    nova_panel_tuck(p);
}

static void panel_delete(lv_event_t *e)
{
    nova_panel_t *p = lv_event_get_user_data(e);
    lv_anim_delete(p, NULL);
    if(p->tuck_timer) lv_timer_delete(p->tuck_timer);
    hold_boost(p, false);
    lv_free(p);
}

/* ---------- construction ---------- */

/* Thin line icons for the dock's buttons: round-capped strokes, no font glyphs. */
static lv_obj_t *icon_box(lv_obj_t *btn)
{
    lv_obj_t *o = ui_plain(btn);
    lv_obj_set_size(o, DOCK_ICON, DOCK_ICON);
    lv_obj_center(o);
    return o;
}

static lv_obj_t *icon_stroke(lv_obj_t *box, const lv_point_precise_t *pts, int32_t w)
{
    lv_obj_t *l = lv_line_create(box);
    lv_line_set_points(l, pts, 2);
    lv_obj_set_style_line_width(l, w, 0);
    lv_obj_set_style_line_color(l, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_line_rounded(l, true, 0);
    lv_obj_set_clickable(l, false);
    return l;
}

static void icon_mic(lv_obj_t *btn)
{
    static const lv_point_precise_t stem[2] = { { 11, 18 }, { 11, 21 } };
    lv_obj_t *b = icon_box(btn);
    lv_obj_t *cap = ui_plain(b);
    lv_obj_set_size(cap, 8, 13);
    lv_obj_set_style_radius(cap, 4, 0);
    lv_obj_set_style_border_width(cap, 2, 0);
    lv_obj_set_style_border_color(cap, cb_col(CB_TEXT_HI), 0);
    lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_t *u = lv_arc_create(b);
    lv_obj_remove_style_all(u);
    lv_obj_set_size(u, 18, 18);
    lv_obj_set_clickable(u, false);
    lv_arc_set_bg_angles(u, 0, 180);
    lv_obj_set_style_arc_width(u, 2, 0);
    lv_obj_set_style_arc_color(u, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_arc_rounded(u, true, 0);
    lv_obj_align(u, LV_ALIGN_TOP_MID, 0, 3);
    icon_stroke(b, stem, 2);
}

static void icon_cross(lv_obj_t *btn)
{
    static const lv_point_precise_t a[2] = { { 6, 6 }, { 16, 16 } }, b[2] = { { 16, 6 }, { 6, 16 } };
    lv_obj_t *o = icon_box(btn);
    icon_stroke(o, a, 2);
    icon_stroke(o, b, 2);
}

/* The tuck button: a bar while open, a plus (the vertical added) while tucked. */
static lv_obj_t *icon_tuck(lv_obj_t *btn)
{
    static const lv_point_precise_t h[2] = { { 5, 11 }, { 17, 11 } }, v[2] = { { 11, 5 }, { 11, 17 } };
    lv_obj_t *o = icon_box(btn);
    icon_stroke(o, h, 2);
    return icon_stroke(o, v, 2);
}

/* The avatar's box is larger than its slot (the head sits in the middle of it), and the dot draws past the slot:
   an overflow-visible parent still clips its children to its own ext draw size, which is 0 unless asked. */
static void ext_draw_cb(lv_event_t *e) { lv_event_set_ext_draw_size(e, (int32_t)(intptr_t)lv_event_get_user_data(e)); }

static void overflow(lv_obj_t *o, int32_t ext)
{
    lv_obj_set_overflow_visible(o, true);
    lv_obj_add_event_cb(o, ext_draw_cb, LV_EVENT_REFR_EXT_DRAW_SIZE, (void *)(intptr_t)ext);
    lv_obj_refresh_ext_draw_size(o);
}

static nova_panel_t *build(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data, bool dock)
{
    nova_panel_t *p = lv_malloc_zeroed(sizeof(*p));
    p->dock = dock;
    LV_ASSERT_MALLOC(p);
    p->fsm = fsm;
    p->on_fx = on_fx;
    p->ud = user_data;
    p->opa = 255;
    p->last_act = fsm->activity;

    p->box = ui_plain(parent);
    lv_obj_set_width(p->box, lv_pct(100));
    lv_obj_set_style_bg_color(p->box, lv_color_hex(dock ? 0x141821 : 0x0e1117), 0);
    lv_obj_set_style_bg_opa(p->box, dock ? (s_dock_blur ? DOCK_BLUR_OPA : DOCK_OPA) : 225, 0);
    lv_obj_set_style_border_color(p->box, dock ? lv_color_hex(GLASS_EDGE) : lv_color_white(), 0);
    lv_obj_set_style_border_opa(p->box, dock ? 130 : 23, 0);
    lv_obj_set_style_border_width(p->box, 1, 0);
    lv_obj_set_style_radius(p->box, dock ? 36 : 26, 0);
    lv_obj_set_style_pad_all(p->box, dock ? 8 : PAD, 0);
    if(dock) lv_obj_set_style_pad_hor(p->box, DOCK_PAD_H, 0);
    lv_obj_set_flex_flow(p->box, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(p->box, dock ? DOCK_GAP : 14, 0);   /* the row's margin leaves room for the avatar */
#if CB_RENDER_GPU
    if(!dock || s_dock_blur) {
        lv_obj_set_style_blur_backdrop(p->box, true, 0);
        lv_obj_set_style_blur_radius(p->box, dock ? s_dock_blur : 24, 0);
    }
#endif
    if(dock) {
        lv_obj_set_flex_align(p->box, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        overflow(p->box, DOCK_AVATAR_BOX);
        lv_obj_align(p->box, LV_ALIGN_BOTTOM_MID, 0, -DOCK_BOTTOM);
    }

    lv_obj_t *top = p->top = ui_row(p->box, 16);
    lv_obj_t *wrap = ui_plain(top);
    if(dock) {
        overflow(top, DOCK_AVATAR_BOX);
        lv_obj_set_size(wrap, DOCK_FACE, DOCK_ROW_H);
        overflow(wrap, DOCK_AVATAR_BOX);
        p->av = avatar_create(wrap, DOCK_AVATAR_BOX);
        lv_obj_align(p->av, LV_ALIGN_CENTER, 0, DOCK_AV_DY);
    }
    else {
        overflow(top, (AVATAR_BOX - FACE) / 2 + 4);
        lv_obj_set_size(wrap, FACE, FACE);
        overflow(wrap, (AVATAR_BOX - FACE) / 2 + 4);
        p->av = avatar_create(wrap, AVATAR_BOX);
        lv_obj_center(p->av);
    }
    lv_obj_set_clickable(wrap, true);   /* the slot is the tap target, not the whole drawing box */
    lv_obj_add_event_cb(wrap, avatar_click, LV_EVENT_CLICKED, p);
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
    if(dock) {
        lv_obj_set_style_text_font(p->state_lbl, cb_font(CB_F_LABEL), 0);
        p->line_clip = ui_plain(p->text_col);
        lv_obj_set_size(p->line_clip, lv_pct(100), lv_font_get_line_height(cb_font(CB_F_TITLE)));
        p->line_lbl = lv_label_create(p->line_clip);
        lv_obj_set_width(p->line_lbl, LV_SIZE_CONTENT);
        lv_label_set_long_mode(p->line_lbl, LV_LABEL_LONG_MODE_CLIP);
        lv_obj_set_style_text_font(p->line_lbl, cb_font(CB_F_TITLE), 0);
    }
    else {
        p->line_lbl = lv_label_create(p->text_col);
        lv_obj_set_width(p->line_lbl, lv_pct(100));
        lv_obj_set_style_text_font(p->line_lbl, cb_font(CB_F_VALUE), 0);
        lv_label_set_long_mode(p->line_lbl, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);   /* a long question slides by */
        lv_obj_set_style_anim_duration(p->line_lbl, lv_anim_speed_clamped(MARQUEE_PX_S, 300, 10000), 0);
    }

    p->ctrls = ui_row(top, 10);
    lv_obj_set_width(p->ctrls, LV_SIZE_CONTENT);
    int32_t btn = dock ? DOCK_BTN : BTN;
    p->mic = nova_bar_round_button(p->ctrls, btn, lv_color_white(), dock ? 26 : 20);
    if(dock) icon_mic(p->mic);
    else nova_bar_mic_glyph(p->mic);
    p->slash = lv_line_create(p->mic);
    p->slash_pts[0] = (lv_point_precise_t){ btn / 4, btn / 4 };
    p->slash_pts[1] = (lv_point_precise_t){ btn - btn / 4, btn - btn / 4 };
    lv_line_set_points(p->slash, p->slash_pts, 2);
    lv_obj_set_size(p->slash, btn, btn);
    lv_obj_set_style_line_width(p->slash, 2, 0);
    lv_obj_set_style_line_color(p->slash, cb_col(CB_RED), 0);
    lv_obj_set_style_line_rounded(p->slash, true, 0);
    lv_obj_set_clickable(p->slash, false);
    lv_obj_add_event_cb(p->mic, mic_click, LV_EVENT_CLICKED, p);
    p->end = nova_bar_round_button(p->ctrls, btn, cb_col(CB_RED), dock ? 255 : 230);
    if(dock) icon_cross(p->end);
    else nova_bar_glyph(p->end, "\xC3\x97", CB_TEXT_HI);
    lv_obj_add_event_cb(p->end, end_click, LV_EVENT_CLICKED, p);
    p->tuck = nova_bar_round_button(p->ctrls, btn, lv_color_white(), dock ? 26 : 20);
    if(dock) p->tuck_v = icon_tuck(p->tuck);
    else nova_bar_glyph(p->tuck, "\xE2\x88\x92", CB_TEXT_2);
    lv_obj_add_event_cb(p->tuck, tuck_click, LV_EVENT_CLICKED, p);

    p->reply = lv_label_create(p->box);
    lv_obj_set_size(p->reply, lv_pct(100), (dock ? DOCK_REPLY_LINES : REPLY_LINES) * lv_font_get_line_height(cb_font(dock ? CB_F_TITLE : CB_F_BODY)));
    lv_label_set_long_mode(p->reply, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_font(p->reply, cb_font(dock ? CB_F_TITLE : CB_F_BODY), 0);
    lv_obj_set_style_text_color(p->reply, cb_col(CB_TEXT_HI), 0);
    lv_label_set_text(p->reply, "");
    if(dock) lv_obj_set_style_text_line_space(p->reply, 4, 0);
    p->table = ui_col(p->box, dock ? 0 : 6);
    if(dock) {
        lv_obj_set_style_radius(p->table, 18, 0);
        lv_obj_set_style_bg_color(p->table, lv_color_hex(0x07080b), 0);
        lv_obj_set_style_bg_opa(p->table, 128, 0);
        lv_obj_set_style_border_color(p->table, lv_color_white(), 0);
        lv_obj_set_style_border_opa(p->table, 20, 0);
        lv_obj_set_style_border_width(p->table, 1, 0);
        lv_obj_set_style_pad_hor(p->table, 14, 0);
        lv_obj_set_style_pad_top(p->table, 12, 0);
        lv_obj_set_style_pad_bottom(p->table, 10, 0);
        p->bubble_row = ui_row(p->box, 0);
        lv_obj_set_flex_align(p->bubble_row, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_t *b = ui_plain(p->bubble_row);
        lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_hor(b, BUBBLE_PAD_H, 0);
        lv_obj_set_style_pad_ver(b, 10, 0);
        lv_obj_set_style_radius(b, 18, 0);   /* LVGL has one radius: the mockup's small tail corner is not drawn */
        lv_obj_set_style_bg_color(b, cb_col(CB_CYAN), 0);
        lv_obj_set_style_bg_opa(b, 41, 0);
        lv_obj_set_style_border_color(b, cb_col(CB_CYAN), 0);
        lv_obj_set_style_border_opa(b, 77, 0);
        lv_obj_set_style_border_width(b, 1, 0);
        p->bubble_lbl = lv_label_create(b);
        lv_label_set_long_mode(p->bubble_lbl, LV_LABEL_LONG_MODE_DOTS);
        lv_obj_set_style_text_font(p->bubble_lbl, cb_font(CB_F_VALUE), 0);
        lv_obj_set_style_text_color(p->bubble_lbl, cb_col(CB_TEXT_HI), 0);
        lv_obj_move_to_index(p->bubble_row, 0);
        lv_obj_move_to_index(top, -1);   /* the row stays at the bottom, the answer grows above it */

        /* faked glass: a state-coloured wash from the top-left corner and a 1 px highlight on the top edge,
           both under the content and neither animated */
        p->wash = ui_plain(p->box);
        lv_obj_set_floating(p->wash, true);
        lv_obj_set_style_radius(p->wash, 36, 0);
        lv_obj_set_style_bg_opa(p->wash, LV_OPA_COVER, 0);
        p->hilite = ui_plain(p->box);
        lv_obj_set_floating(p->hilite, true);
        lv_obj_set_style_bg_color(p->hilite, lv_color_white(), 0);
        lv_obj_set_style_bg_opa(p->hilite, 20, 0);
        lv_obj_move_to_index(p->wash, 0);
        lv_obj_move_to_index(p->hilite, 1);
        lv_obj_add_event_cb(p->box, box_size_cb, LV_EVENT_SIZE_CHANGED, p);
    }

    p->target = fsm->view;
    apply_layout(p, fsm->view);
    refresh_state(p);
    set_height(p, measure(p));
    lv_obj_add_event_cb(p->box, panel_delete, LV_EVENT_DELETE, p);
    return p;
}

nova_panel_t *nova_panel_create(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data)
{
    return build(parent, fsm, on_fx, user_data, false);
}

nova_panel_t *nova_panel_create_dock(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data)
{
    return build(parent, fsm, on_fx, user_data, true);
}

bool nova_panel_expanded(const nova_panel_t *p) { return p->target == NV_VIEW_OPEN; }

void nova_panel_tuck(nova_panel_t *p)
{
    if(!p || !p->dock) return;
    cancel_tuck(p);
    tuck_via_fsm(p);
}

lv_obj_t *nova_panel_button(nova_panel_t *p, nova_btn_t b) { return b == NOVA_BTN_MIC ? p->mic : b == NOVA_BTN_END ? p->end : p->tuck; }

lv_obj_t *nova_panel_card(nova_panel_t *p) { return p->table; }

void nova_panel_set_dock_blur(int radius) { s_dock_blur = radius; }

lv_obj_t *nova_panel_reply(nova_panel_t *p) { return p->reply; }

lv_obj_t *nova_panel_bubble(nova_panel_t *p) { return p->bubble_lbl ? lv_obj_get_parent(p->bubble_lbl) : NULL; }

void nova_panel_set_reduce_motion(nova_panel_t *p, bool on)
{
    p->reduce = on;
    if(p->dock) marquee_restart(p);
}

void nova_panel_set_max_height(nova_panel_t *p, int32_t h) { p->max_h = h; }

lv_obj_t *nova_panel_obj(nova_panel_t *p) { return p->box; }

lv_obj_t *nova_panel_avatar(nova_panel_t *p) { return p->av; }

lv_obj_t *nova_panel_line(nova_panel_t *p) { return p->line_clip ? p->line_clip : p->line_lbl; }

bool nova_panel_is_animating(const nova_panel_t *p) { return p->phase != PH_NONE; }

bool nova_panel_notified(const nova_panel_t *p) { return p->notified; }
