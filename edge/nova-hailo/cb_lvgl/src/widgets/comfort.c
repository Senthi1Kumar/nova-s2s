#include "widgets/comfort.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "widgets/ui_kit.h"
#include "cb_ambient.h"
#include <math.h>
#include <stdint.h>

#define DOT "\xC2\xB7"
#define DEG "\xC2\xB0"

static bool s_ready;   /* observers fire once when attached; tiles only flash on later changes */

static lv_obj_t *box(lv_obj_t *parent, lv_flex_flow_t flow, lv_flex_align_t cross)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(b, flow);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, cross, cross);
    return b;
}

/* ---------- climate ---------- */

static lv_obj_t *s_fan[5], *s_fan_lbl;
static int32_t s_fan_on;   /* bars lit; they pulse on the ambient clock */

static void fan_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    int32_t n = lv_subject_get_int(s);
    s_fan_on = LV_CLAMP(0, n, 5);
    for(int32_t i = 0; i < 5; i++) lv_obj_set_style_bg_opa(s_fan[i], i < n ? LV_OPA_COVER : 40, 0);
    lv_label_set_text_fmt(s_fan_lbl, "Fan %d", (int)n);
}

static void temp_obs(lv_observer_t *o, lv_subject_t *s)
{
    lv_obj_t *l = lv_observer_get_target_obj(o);
    int32_t dc = lv_subject_get_int(s);
    if(dc % 10) lv_label_set_text_fmt(l, "%d.%d" DEG, (int)(dc / 10), (int)(dc % 10));
    else lv_label_set_text_fmt(l, "%d" DEG, (int)(dc / 10));
}

static void ac_obs(lv_observer_t *o, lv_subject_t *s)
{
    lv_obj_t *chip = lv_observer_get_target_obj(o);
    bool on = lv_subject_get_int(s) != 0;
    lv_label_set_text(ui_chip_label(chip), on ? "A/C on" : "A/C off");
    lv_obj_set_style_bg_color(lv_obj_get_child(chip, 0), cb_col(on ? CB_CYAN : CB_TEXT_MID), 0);
}

static lv_obj_t *temp_block(lv_obj_t *row, const char *who, cb_color_t c, bool right)
{
    lv_obj_t *b = box(row, LV_FLEX_FLOW_COLUMN, right ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START);
    ui_label(b, who, CB_F_SMALL, CB_TEXT_LO);
    return ui_label(b, "", CB_F_TEMP, c);
}

static void climate_card(lv_obj_t *col)
{
    cb_model_t *m = cb_model();
    lv_obj_t *card = ui_card(col);

    lv_obj_t *head = ui_row(card, 6);
    ui_label(head, "Climate", CB_F_BODY, CB_TEXT_MID);
    ui_spacer(head);
    lv_obj_t *ac = ui_chip(head, "A/C on", CB_CYAN, false);
    ui_chip(head, "Auto", CB_TEXT_MID, false);
    lv_subject_add_observer_obj(m->ac_on, ac_obs, ac, NULL);

    lv_obj_t *row = ui_row(card, 0);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_t *drv = temp_block(row, "Driver", CB_TEXT_HI, false);
    ui_spacer(row);
    lv_obj_t *fan = box(row, LV_FLEX_FLOW_COLUMN, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(fan, 5, 0);
    lv_obj_set_style_pad_bottom(fan, 6, 0);
    lv_obj_t *bars = box(fan, LV_FLEX_FLOW_ROW, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(bars, 4, 0);
    for(int i = 0; i < 5; i++) {
        s_fan[i] = ui_plain(bars);
        lv_obj_set_size(s_fan[i], 6, 16);
        lv_obj_set_style_radius(s_fan[i], 2, 0);
        lv_obj_set_style_bg_color(s_fan[i], cb_col(CB_CYAN), 0);
    }
    s_fan_lbl = ui_label(fan, "", CB_F_SMALL, CB_TEXT_LO);
    ui_spacer(row);
    lv_obj_t *pas = temp_block(row, "Passenger", CB_TEXT_2, true);

    lv_subject_add_observer_obj(m->fan_level, fan_obs, bars, NULL);
    lv_subject_add_observer_obj(m->driver_temp_dc, temp_obs, drv, NULL);
    lv_subject_add_observer_obj(m->passenger_temp_dc, temp_obs, pas, NULL);
}

/* ---------- tiles ---------- */

typedef enum {
    T_WINDOWS, T_SEATS, T_DOORS, T_LIGHTS, T_SUNROOF, T_WIPERS,
    T_DEFROST, T_MIRRORS, T_AMBIENT, T_MODE, T_CABIN, T_DEVICES, T_COUNT
} tile_id_t;

typedef struct {
    tile_id_t id;
    lv_obj_t *box, *val, *g;
    lv_color_t edge;     /* resting border colour */
    lv_opa_t edge_opa;
    int32_t glow;        /* gradient glow progress 0..1000; 0 = none */
} tile_t;

static const struct { const char *label; cb_color_t dot; const char *val; const char *sub; } k_tiles[T_COUNT] = {
    { "WINDOWS", CB_TEXT_MID, "", "4 windows " DOT " rear shade" },
    { "SEATS", CB_YELLOW, "", "driver " DOT " vent off" },
    { "DOORS", CB_GREEN, "", "child lock on" },
    { "LIGHTS", CB_CYAN, "Auto", "low beam " DOT " hazards off" },
    { "SUNROOF", CB_YELLOW, "", "shade open" },
    { "WIPERS", CB_TEXT_MID, "", "rain sensor ready" },
    { "DEFROST", CB_TEXT_MID, "Off", "front " DOT " rear" },
    { "MIRRORS", CB_TEXT_MID, "Unfolded", "auto-fold on lock" },
    { "AMBIENT", CB_GREEN, "Teal " DOT " 60%", "7 colours" },
    { "DRIVE MODE", CB_GREEN, "Eco", "eco " DOT " comfort " DOT " sport" },
    { "CABIN", CB_TEXT_MID, "", "1 occupant " DOT " 18" DEG " out" },
    { "ALL DEVICES", CB_VIOLET, "30 modules", "VehicleWorld map \xE2\x86\x92" },
};

static tile_t s_tiles[T_COUNT];
static int32_t s_glow = LV_OPA_COVER;   /* shared 3 s breathing level for seat heat, light rays, ambient halo */
static int32_t s_blade = -60;           /* wiper blade angle, degrees from vertical */
static bool s_wiping;

/* Mini-graphics are drawn in a 26x22 box with the artifact's SVG coordinates (1 unit = 1 px). */
typedef struct { lv_layer_t *L; float x, y; } gc_t;

static void g_rect(const gc_t *g, float x, float y, float w, float h, int32_t r, lv_color_t c, lv_opa_t opa)
{
    if(w < 1 || h < 1) return;
    lv_draw_rect_dsc_t d;
    lv_draw_rect_dsc_init(&d);
    d.bg_color = c;
    d.bg_opa = opa;
    d.radius = r;
    lv_area_t a = { (int32_t)(g->x + x), (int32_t)(g->y + y), (int32_t)(g->x + x + w) - 1, (int32_t)(g->y + y + h) - 1 };
    lv_draw_rect(g->L, &d, &a);
}

static void g_frame(const gc_t *g, float x, float y, float w, float h, int32_t r, lv_color_t c, lv_opa_t opa)
{
    lv_draw_rect_dsc_t d;
    lv_draw_rect_dsc_init(&d);
    d.bg_opa = LV_OPA_TRANSP;
    d.border_color = c;
    d.border_opa = opa;
    d.border_width = 1;
    d.radius = r;
    lv_area_t a = { (int32_t)(g->x + x), (int32_t)(g->y + y), (int32_t)(g->x + x + w) - 1, (int32_t)(g->y + y + h) - 1 };
    lv_draw_rect(g->L, &d, &a);
}

static void g_poly(const gc_t *g, const float (*pts)[2], int32_t n, int32_t w, lv_color_t c, lv_opa_t opa)
{
    lv_point_precise_t p[8];
    if(n > 8) n = 8;
    for(int32_t i = 0; i < n; i++) p[i] = (lv_point_precise_t){ g->x + pts[i][0], g->y + pts[i][1] };
    lv_draw_line_dsc_t d;
    lv_draw_line_dsc_init(&d);
    d.points = p;
    d.point_cnt = n;
    d.width = w;
    d.color = c;
    d.opa = opa;
    d.round_start = 1;
    d.round_end = 1;
    lv_draw_line(g->L, &d);
}

static void g_arc(const gc_t *g, float cx, float cy, uint16_t r, int32_t w, float a0, float a1, lv_color_t c, lv_opa_t opa)
{
    lv_draw_arc_dsc_t d;
    lv_draw_arc_dsc_init(&d);
    d.center = (lv_point_t){ (int32_t)(g->x + cx), (int32_t)(g->y + cy) };
    d.radius = r;
    d.width = w;
    d.start_angle = a0;
    d.end_angle = a1;
    d.color = c;
    d.opa = opa;
    lv_draw_arc(g->L, &d);
}

static lv_opa_t scale(lv_opa_t base, int32_t level) { return (lv_opa_t)(base * level / 255); }

static void tile_draw(lv_event_t *e)
{
    lv_obj_t *obj = lv_event_get_target_obj(e);
    const tile_t *t = lv_event_get_user_data(e);
    lv_area_t a;
    lv_obj_get_coords(obj, &a);
    gc_t g = { lv_event_get_layer(e), (float)a.x1, (float)a.y1 };
    cb_model_t *m = cb_model();
    lv_color_t white = lv_color_white(), cyan = cb_col(CB_CYAN), green = cb_col(CB_GREEN);
    lv_color_t orange = cb_col(CB_ORANGE), mid = cb_col(CB_TEXT_MID);

    switch(t->id) {
    case T_WINDOWS:
        for(int i = 0; i < CB_WIN_COUNT; i++) {
            float h = 18.0f * (float)lv_subject_get_int(m->win_pct[i]) / 100.0f;
            g_rect(&g, (float)(i * 7), 2, 5, 18, 2, white, 26);
            g_rect(&g, (float)(i * 7), 2 + 18 - h, 5, h, 2, green, LV_OPA_COVER);
        }
        break;
    case T_SEATS: {
        bool heat = lv_subject_get_int(m->seat_heat) > 0;
        g_rect(&g, 2, 12, 6, 8, 2, heat ? orange : white, heat ? LV_OPA_COVER : 31);
        g_rect(&g, 10, 12, 6, 8, 2, white, 31);
        g_rect(&g, 18, 12, 6, 8, 2, white, 31);
        static const float wave[][2] = { { 5, 9 }, { 3.6f, 6 }, { 5, 3 } };
        if(heat) g_poly(&g, wave, 3, 2, orange, scale(LV_OPA_COVER, s_glow));
        break;
    }
    case T_DOORS: {
        bool locked = lv_subject_get_int(m->locked) != 0;
        lv_color_t c = locked ? green : orange;
        float lift = locked ? 0 : 3;
        const float shackle[][2] = { { 10, 10 - lift }, { 10, 7 - lift }, { 11, 5 - lift }, { 13, 4.5f - lift },
                                     { 15, 5 - lift }, { 16, 7 - lift }, { 16, 10 - lift } };
        g_poly(&g, shackle, 7, 2, c, LV_OPA_COVER);
        g_rect(&g, 7, 10, 12, 9, 2, c, LV_OPA_COVER);
        break;
    }
    case T_LIGHTS: {
        g_rect(&g, 2, 6, 8, 10, 5, cyan, LV_OPA_COVER);
        g_rect(&g, 2, 6, 4, 10, 0, cyan, LV_OPA_COVER);
        static const float r1[][2] = { { 13, 7 }, { 24, 7 } }, r2[][2] = { { 13, 11 }, { 25, 11 } }, r3[][2] = { { 13, 15 }, { 24, 15 } };
        lv_opa_t o = scale(179, s_glow);
        g_poly(&g, r1, 2, 2, cyan, o);
        g_poly(&g, r2, 2, 2, cyan, o);
        g_poly(&g, r3, 2, 2, cyan, o);
        break;
    }
    case T_SUNROOF:
        g_rect(&g, 3, 3, 20.0f * (float)lv_subject_get_int(m->sunroof_pct) / 100.0f, 16, 3, cyan, 153);
        g_frame(&g, 3, 3, 20, 16, 3, white, 51);
        break;
    case T_WIPERS: {
        g_arc(&g, 13, 20, 11, 2, 180, 360, white, 36);
        float s = (float)lv_trigo_sin((int16_t)s_blade) / LV_TRIGO_SIN_MAX;
        float c = (float)lv_trigo_cos((int16_t)s_blade) / LV_TRIGO_SIN_MAX;
        const float blade[][2] = { { 13, 20 }, { 13 + 15 * s, 20 - 15 * c } };
        g_poly(&g, blade, 2, 2, cyan, LV_OPA_COVER);
        break;
    }
    case T_DEFROST:
        for(int i = 0; i < 3; i++) {
            float x = (float)(5 + i * 8);
            const float w[][2] = { { x, 4 }, { x + 2, 6.5f }, { x, 11 }, { x - 2, 15.5f }, { x, 18 } };
            g_poly(&g, w, 5, 1, white, 51);
        }
        break;
    case T_MIRRORS: {
        static const float l[][2] = { { 1, 6 }, { 10, 6 }, { 10, 15 }, { 4, 15 }, { 1, 6 } };
        static const float r[][2] = { { 25, 6 }, { 16, 6 }, { 16, 15 }, { 22, 15 }, { 25, 6 } };
        g_poly(&g, l, 5, 1, mid, LV_OPA_COVER);
        g_poly(&g, r, 5, 1, mid, LV_OPA_COVER);
        break;
    }
    case T_AMBIENT: {
        static const cb_color_t cols[] = {
            CB_TEXT_HI, CB_CYAN, CB_RED, CB_GREEN, CB_VIOLET, CB_ORANGE, CB_RED
        };
        lv_color_t c = cb_col(cols[LV_CLAMP(0, lv_subject_get_int(m->ambient_color), 6)]);
        g_rect(&g, 4, 2, 18, 18, LV_RADIUS_CIRCLE, c, scale(89, s_glow));
        g_rect(&g, 8, 6, 10, 10, LV_RADIUS_CIRCLE, c, LV_OPA_COVER);
        break;
    }
    case T_MODE: {
        int32_t mode = LV_CLAMP(0, lv_subject_get_int(m->drive_mode), 2);
        for(int i = 0; i < 3; i++) {
            g_rect(&g, (float)(1 + i * 9), 9, 7, 4, 2, i == mode ? green : white, i == mode ? LV_OPA_COVER : 31);
        }
        break;
    }
    case T_CABIN: {
        float h = 13.0f * (float)(lv_subject_get_int(m->driver_temp_dc) - 150) / 150.0f;
        if(h > 13) h = 13;
        g_rect(&g, 11, 2, 4, 13, 2, white, 31);
        g_rect(&g, 11, 15 - h, 4, h, 2, orange, LV_OPA_COVER);
        g_rect(&g, 9.5f, 13.5f, 7, 7, LV_RADIUS_CIRCLE, orange, LV_OPA_COVER);
        break;
    }
    case T_DEVICES:
        g_arc(&g, 13, 11, 9, 3, 0, 360, white, 31);
        g_arc(&g, 13, 11, 9, 3, 270, 270 + 360.0f * 19 / 30, cb_col(CB_VIOLET), LV_OPA_COVER);
        break;
    default:
        break;
    }
}

static void flash_exec(void *var, int32_t v)
{
    const tile_t *t = lv_obj_get_user_data(var);
    lv_obj_set_style_border_color(var, lv_color_mix(cb_col(CB_CYAN), t->edge, (uint8_t)v), 0);
    lv_obj_set_style_border_opa(var, (lv_opa_t)(t->edge_opa + (153 - t->edge_opa) * v / 255), 0);
    lv_obj_set_style_bg_color(var, lv_color_mix(cb_col(CB_CYAN), lv_color_white(), (uint8_t)v), 0);
    lv_obj_set_style_bg_opa(var, (lv_opa_t)(9 + 11 * v / 255), 0);
}

/* ---------- gradient glow (layout v2) ---------- */

#define GLOW_MS 1500
static comfort_flash_t s_flash_style = COMFORT_FLASH_TINT;

void comfort_set_flash_style(comfort_flash_t style) { s_flash_style = style; }

/* Cyan -> violet -> pink -> cyan around the colour wheel; u in turns. */
static lv_color_t sweep(float u)
{
    static const uint32_t k[3] = { 0x4cd6ff, 0xb66bff, 0xff6bd5 };
    float x = (u - floorf(u)) * 3.0f;
    int i = (int)x > 2 ? 2 : (int)x;
    return lv_color_mix(lv_color_hex(k[(i + 1) % 3]), lv_color_hex(k[i]), (uint8_t)((x - (float)i) * 255.0f));
}

static float glow_env(int32_t v)   /* fade in over 12 %, hold, fade out over the last 35 % */
{
    float q = (float)v / 1000.0f;
    return q < 0.12f ? q / 0.12f : q > 0.65f ? (1.0f - q) / 0.35f : 1.0f;
}

static void grad_strip(lv_layer_t *L, int32_t x1, int32_t y1, int32_t x2, int32_t y2, lv_color_t a, lv_color_t b,
                       bool hor, lv_opa_t opa)
{
    if(x2 < x1 || y2 < y1) return;
    lv_draw_rect_dsc_t d;
    lv_draw_rect_dsc_init(&d);
    d.bg_opa = opa;
    lv_color_t cs[2] = { a, b };
    lv_opa_t os[2] = { LV_OPA_COVER, LV_OPA_COVER };
    uint8_t fs[2] = { 0, 255 };
    lv_grad_init_stops(&d.bg_grad, cs, os, fs, 2);
    if(hor) lv_grad_horizontal_init(&d.bg_grad);
    else lv_grad_vertical_init(&d.bg_grad);
    lv_area_t ar = { x1, y1, x2, y2 };
    lv_draw_rect(L, &d, &ar);
}

static void corner(lv_layer_t *L, int32_t cx, int32_t cy, int32_t r, int32_t w, int32_t a0, lv_color_t c, lv_opa_t opa)
{
    lv_draw_arc_dsc_t d;
    lv_draw_arc_dsc_init(&d);
    d.center = (lv_point_t){ cx, cy };
    d.radius = (uint16_t)r;
    d.width = w;
    d.start_angle = a0;
    d.end_angle = a0 + 90;
    d.color = c;
    d.opa = opa;
    lv_draw_arc(L, &d);
}

/* A rounded ring inset by `in`, `w` px wide: corners c[0..3] clockwise from the top left, the straight runs
   blending from corner to corner, so the whole border reads as one travelling gradient. */
static void ring(lv_layer_t *L, const lv_area_t *a, int32_t in, int32_t w, int32_t r, const lv_color_t c[4],
                 lv_opa_t opa)
{
    int32_t x1 = a->x1 + in, y1 = a->y1 + in, x2 = a->x2 - in, y2 = a->y2 - in;
    r = r - in < w ? w : r - in;
    grad_strip(L, x1 + r, y1, x2 - r, y1 + w - 1, c[0], c[1], true, opa);
    grad_strip(L, x2 - w + 1, y1 + r, x2, y2 - r, c[1], c[2], false, opa);
    grad_strip(L, x1 + r, y2 - w + 1, x2 - r, y2, c[3], c[2], true, opa);
    grad_strip(L, x1, y1 + r, x1 + w - 1, y2 - r, c[0], c[3], false, opa);
    corner(L, x1 + r, y1 + r, r, w, 180, c[0], opa);
    corner(L, x2 - r, y1 + r, r, w, 270, c[1], opa);
    corner(L, x2 - r, y2 - r, r, w, 0, c[2], opa);
    corner(L, x1 + r, y2 - r, r, w, 90, c[3], opa);
}

static void glow_draw(lv_event_t *e)
{
    lv_obj_t *box = lv_event_get_current_target_obj(e);
    const tile_t *t = lv_obj_get_user_data(box);
    if(t->glow <= 0) return;
    float env = glow_env(t->glow);
    float turn = (float)t->glow / 1000.0f * 1.25f;   /* a turn and a quarter over the glow */
    lv_color_t c[4];
    for(int i = 0; i < 4; i++) c[i] = sweep(turn + 0.25f * (float)i);
    lv_area_t a;
    lv_obj_get_coords(box, &a);
    lv_layer_t *L = lv_event_get_layer(e);
    ring(L, &a, 0, 2, 12, c, (lv_opa_t)(255.0f * env));
    ring(L, &a, 2, 4, 12, c, (lv_opa_t)(70.0f * env));   /* soft light just inside the edge */
}

static void glow_exec(void *var, int32_t v)
{
    tile_t *t = lv_obj_get_user_data(var);
    t->glow = v;
    float env = glow_env(v);
    lv_obj_set_style_shadow_color(var, sweep((float)v / 1000.0f * 1.25f), 0);
    lv_obj_set_style_shadow_width(var, (int32_t)(20.0f * env), 0);
    lv_obj_set_style_shadow_opa(var, (lv_opa_t)(120.0f * env), 0);
    lv_obj_invalidate(var);
}

static void glow_done(lv_anim_t *a) { glow_exec(lv_anim_get_user_data(a), 0); }

static void tile_glow(tile_t *t)
{
    lv_anim_delete(t->box, glow_exec);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, t->box);
    lv_anim_set_user_data(&a, t->box);
    lv_anim_set_exec_cb(&a, glow_exec);
    lv_anim_set_values(&a, 1, 1000);
    lv_anim_set_duration(&a, GLOW_MS);
    lv_anim_set_path_cb(&a, lv_anim_path_linear);
    lv_anim_set_completed_cb(&a, glow_done);
    lv_anim_start(&a);
}

/* Cyan border + tint for 2.2 s: 0.4 s in, hold, 0.4 s out (instant in and out under reduce-motion). */
static void tile_flash(tile_t *t)
{
    if(!s_ready) return;
    bool rm = cb_reduce_motion();
    if(s_flash_style == COMFORT_FLASH_GRADIENT && !rm) {
        tile_glow(t);
        return;
    }
    lv_anim_delete(t->box, flash_exec);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, t->box);
    lv_anim_set_exec_cb(&a, flash_exec);
    lv_anim_set_values(&a, 0, 255);
    lv_anim_set_duration(&a, rm ? 1 : 400);
    lv_anim_set_reverse_duration(&a, rm ? 1 : 400);
    lv_anim_set_reverse_delay(&a, rm ? 2200 : 1400);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_start(&a);
}


/* Recompute a tile's value text from the model and redraw its graphic. */
static void tile_refresh(tile_t *t)
{
    cb_model_t *m = cb_model();
    switch(t->id) {
    case T_WINDOWS: {
        int open = 0;
        for(int i = 0; i < CB_WIN_COUNT; i++) open += lv_subject_get_int(m->win_pct[i]) > 0;
        if(open) lv_label_set_text_fmt(t->val, "%d open", open);
        else lv_label_set_text(t->val, "All closed");
        break;
    }
    case T_SEATS: {
        static const char *const lv[] = { "Heat " DOT " off", "Heat " DOT " low", "Heat " DOT " mid", "Heat " DOT " high" };
        int32_t h = LV_CLAMP(0, lv_subject_get_int(m->seat_heat), 3);
        lv_label_set_text(t->val, lv[h]);
        break;
    }
    case T_DOORS:
        lv_label_set_text(t->val, lv_subject_get_int(m->locked) ? "Locked" : "Unlocked");
        break;
    case T_SUNROOF: {
        int32_t p = lv_subject_get_int(m->sunroof_pct);
        lv_label_set_text(t->val, p >= 100 ? "Open" : p > 0 ? "Tilted" : "Closed");
        break;
    }
    case T_WIPERS: {
        static const char *const names[] = { "Off", "Auto", "Low", "High" };
        int32_t w = LV_CLAMP(0, lv_subject_get_int(m->wiper_mode), 3);
        lv_label_set_text(t->val, names[w]);
        s_wiping = w != 0;
        if(!s_wiping) s_blade = -60;
        break;
    }
    case T_LIGHTS: {
        static const char *const names[] = { "Off", "Auto", "Low beam", "High beam" };
        lv_label_set_text(t->val, names[LV_CLAMP(0, lv_subject_get_int(m->headlights), 3)]);
        break;
    }
    case T_DEFROST: {
        int32_t d = lv_subject_get_int(m->defrost);
        const char *txt = d == 3 ? "Front " DOT " rear" : d == 2 ? "Rear" : d == 1 ? "Front" : "Off";
        lv_label_set_text(t->val, txt);
        break;
    }
    case T_MIRRORS:
        lv_label_set_text(t->val, lv_subject_get_int(m->mirrors_folded) ? "Folded" : "Unfolded");
        break;
    case T_AMBIENT: {
        static const char *const names[] = { "White", "Blue", "Red", "Green", "Purple", "Orange", "Pink" };
        int32_t c = LV_CLAMP(0, lv_subject_get_int(m->ambient_color), 6);
        int32_t lvl = LV_CLAMP(0, lv_subject_get_int(m->ambient_level), 100);
        lv_label_set_text_fmt(t->val, "%s " DOT " %d%%", names[c], (int)lvl);
        break;
    }
    case T_MODE: {
        static const char *const names[] = { "Eco", "Comfort", "Sport" };
        lv_label_set_text(t->val, names[LV_CLAMP(0, lv_subject_get_int(m->drive_mode), 2)]);
        break;
    }
    case T_CABIN:
        lv_label_set_text_fmt(t->val, "%d" DEG " inside", (int)((lv_subject_get_int(m->driver_temp_dc) + 15) / 10));
        break;
    default:
        break;
    }
    lv_obj_invalidate(t->g);
}

static void tile_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(s);
    tile_t *t = lv_observer_get_user_data(o);
    tile_refresh(t);
    tile_flash(t);
}

static int32_t s_cols = 4;   /* 3 in a narrow pane (layout B), so labels clear the mini-graphics */

static void tile_create(lv_obj_t *grid, tile_id_t id)
{
    tile_t *t = &s_tiles[id];
    t->id = id;
    bool devices = id == T_DEVICES;
    t->edge = devices ? cb_col(CB_VIOLET) : lv_color_white();
    t->edge_opa = devices ? 89 : 23;

    t->box = ui_plain(grid);
    lv_obj_set_user_data(t->box, t);
    lv_obj_set_grid_cell(t->box, LV_GRID_ALIGN_STRETCH, id % s_cols, 1, LV_GRID_ALIGN_STRETCH, id / s_cols, 1);
    lv_obj_set_style_radius(t->box, 12, 0);
    lv_obj_set_style_border_width(t->box, 1, 0);
    lv_obj_set_style_pad_hor(t->box, 10, 0);
    lv_obj_set_style_pad_ver(t->box, 9, 0);
    lv_obj_set_style_pad_row(t->box, 1, 0);
    lv_obj_set_flex_flow(t->box, LV_FLEX_FLOW_COLUMN);
    flash_exec(t->box, 0);
    if(s_flash_style == COMFORT_FLASH_GRADIENT) lv_obj_add_event_cb(t->box, glow_draw, LV_EVENT_DRAW_POST_END, NULL);

    lv_obj_t *lab = ui_row(t->box, 5);
    ui_dot(lab, k_tiles[id].dot, false);
    lv_obj_t *l = ui_label(lab, k_tiles[id].label, CB_F_SMALL, CB_TEXT_MID);
    lv_obj_set_style_text_letter_space(l, 1, 0);
    ui_spacer(t->box);
    t->val = ui_label(t->box, k_tiles[id].val, CB_F_BODY, CB_TEXT_HI);
    ui_label_line(t->box, k_tiles[id].sub, CB_F_SMALL, CB_TEXT_LO);

    t->g = ui_plain(t->box);
    lv_obj_set_size(t->g, 26, 22);
    lv_obj_set_ignore_layout(t->g, true);
    lv_obj_align(t->g, LV_ALIGN_TOP_RIGHT, 2, 0);
    lv_obj_add_event_cb(t->g, tile_draw, LV_EVENT_DRAW_MAIN_END, t);
}

static void tiles_grid(lv_obj_t *col)
{
    static const int32_t cols4[] = { LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_TEMPLATE_LAST };
    static const int32_t rows3[] = { LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_TEMPLATE_LAST };
    static const int32_t cols3[] = { LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_TEMPLATE_LAST };
    static const int32_t rows4[] = { LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_FR(1), LV_GRID_TEMPLATE_LAST };
    lv_obj_update_layout(col);
    s_cols = lv_obj_get_content_width(col) < 560 ? 3 : 4;
    const int32_t *cols = s_cols == 3 ? cols3 : cols4, *rows = s_cols == 3 ? rows4 : rows3;
    lv_obj_t *grid = ui_plain(col);
    lv_obj_set_width(grid, lv_pct(100));
    lv_obj_set_flex_grow(grid, 1);
    lv_obj_set_grid_dsc_array(grid, cols, rows);
    lv_obj_set_style_pad_column(grid, 8, 0);
    lv_obj_set_style_pad_row(grid, 8, 0);
    for(int i = 0; i < T_COUNT; i++) tile_create(grid, (tile_id_t)i);

    cb_model_t *m = cb_model();
    for(int i = 0; i < CB_WIN_COUNT; i++) lv_subject_add_observer_obj(m->win_pct[i], tile_obs, s_tiles[T_WINDOWS].box, &s_tiles[T_WINDOWS]);
    lv_subject_add_observer_obj(m->seat_heat, tile_obs, s_tiles[T_SEATS].box, &s_tiles[T_SEATS]);
    lv_subject_add_observer_obj(m->locked, tile_obs, s_tiles[T_DOORS].box, &s_tiles[T_DOORS]);
    lv_subject_add_observer_obj(m->sunroof_pct, tile_obs, s_tiles[T_SUNROOF].box, &s_tiles[T_SUNROOF]);
    lv_subject_add_observer_obj(m->wiper_mode, tile_obs, s_tiles[T_WIPERS].box, &s_tiles[T_WIPERS]);
    lv_subject_add_observer_obj(m->headlights, tile_obs, s_tiles[T_LIGHTS].box, &s_tiles[T_LIGHTS]);
    lv_subject_add_observer_obj(m->defrost, tile_obs, s_tiles[T_DEFROST].box, &s_tiles[T_DEFROST]);
    lv_subject_add_observer_obj(m->mirrors_folded, tile_obs, s_tiles[T_MIRRORS].box, &s_tiles[T_MIRRORS]);
    lv_subject_add_observer_obj(m->ambient_color, tile_obs, s_tiles[T_AMBIENT].box, &s_tiles[T_AMBIENT]);
    lv_subject_add_observer_obj(m->ambient_level, tile_obs, s_tiles[T_AMBIENT].box, &s_tiles[T_AMBIENT]);
    lv_subject_add_observer_obj(m->drive_mode, tile_obs, s_tiles[T_MODE].box, &s_tiles[T_MODE]);
    lv_subject_add_observer_obj(m->driver_temp_dc, tile_obs, s_tiles[T_CABIN].box, &s_tiles[T_CABIN]);
}

/* ---------- media ---------- */

typedef enum { BTN_PREV, BTN_PLAY, BTN_NEXT } btn_kind_t;

static lv_obj_t *s_art, *s_eq[4], *s_title, *s_msub, *s_tnow, *s_tdur, *s_prog, *s_play, *s_vol, *s_shuffle;
static bool s_shuffle_on, s_playing;

static void eq_set(lv_obj_t *bar, int32_t h)
{
    if(lv_obj_get_height(bar) == h) return;
    lv_obj_set_height(bar, h);
    lv_obj_set_y(bar, 38 - h);
}

static void media_sub(void)
{
    cb_model_t *m = cb_model();
    lv_label_set_text_fmt(s_msub, "Spotify " DOT " %s " DOT " vol %d",
                          lv_subject_get_int(m->media_playing) ? "playing" : "paused", (int)lv_subject_get_int(m->volume));
}

static void playing_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    bool on = lv_subject_get_int(s) != 0;
    s_playing = on;
    for(int i = 0; i < 4; i++) eq_set(s_eq[i], 6);
    media_sub();
    lv_obj_invalidate(s_play);
}

static void fmt_time(lv_obj_t *l, int32_t s) { lv_label_set_text_fmt(l, "%d:%02d", (int)(s / 60), (int)(s % 60)); }

static void show_track_title(void)
{
    int32_t t = lv_subject_get_int(cb_model()->media_track);
    const char *custom = cb_model_media_title();
    lv_label_set_text(s_title, (custom && custom[0]) ? custom : cb_model_track_title(t));
}

static void track_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    int32_t t = lv_subject_get_int(s);
    int32_t dur = cb_model_track_duration(t);
    show_track_title();
    fmt_time(s_tdur, dur);
    lv_bar_set_range(s_prog, 0, dur);
}

static void title_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    LV_UNUSED(s);
    show_track_title();
}

static void pos_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    int32_t p = lv_subject_get_int(s);
    fmt_time(s_tnow, p);
    lv_bar_set_value(s_prog, p, LV_ANIM_OFF);
}

static void volume_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    lv_label_set_text_fmt(ui_chip_label(s_vol), "Vol %d", (int)lv_subject_get_int(s));
    media_sub();
}

static void glyph_draw(lv_event_t *e)
{
    lv_obj_t *b = lv_event_get_target_obj(e);
    btn_kind_t k = (btn_kind_t)(intptr_t)lv_event_get_user_data(e);
    lv_area_t a;
    lv_obj_get_coords(b, &a);
    /* The button spans [x1, x2 + 1), so its centre is x1 + w/2, not (x1 + x2)/2 (a pixel short). Glyphs live in
       a 14x14 box centred on (7, 7), every edge on a whole pixel so both pause bars come out the same width. */
    int32_t cx = a.x1 + lv_area_get_width(&a) / 2, cy = a.y1 + lv_area_get_height(&a) / 2;
    gc_t g = { lv_event_get_layer(e), (float)(cx - 7), (float)(cy - 7) };
    lv_color_t c = k == BTN_PLAY ? cb_col(CB_BASE) : cb_col(CB_TEXT_HI);

    lv_draw_triangle_dsc_t t;
    lv_draw_triangle_dsc_init(&t);
    t.color = c;
    t.opa = LV_OPA_COVER;
    if(k == BTN_PREV) {
        g_rect(&g, 1, 1, 3, 12, 1, c, LV_OPA_COVER);
        t.p[0] = (lv_point_precise_t){ g.x + 13, g.y + 1 };
        t.p[1] = (lv_point_precise_t){ g.x + 5, g.y + 7 };
        t.p[2] = (lv_point_precise_t){ g.x + 13, g.y + 13 };
        lv_draw_triangle(g.L, &t);
    } else if(k == BTN_NEXT) {
        t.p[0] = (lv_point_precise_t){ g.x + 1, g.y + 1 };
        t.p[1] = (lv_point_precise_t){ g.x + 9, g.y + 7 };
        t.p[2] = (lv_point_precise_t){ g.x + 1, g.y + 13 };
        lv_draw_triangle(g.L, &t);
        g_rect(&g, 10, 1, 3, 12, 1, c, LV_OPA_COVER);
    } else if(lv_subject_get_int(cb_model()->media_playing)) {
        g_rect(&g, 3, 1, 3, 12, 1, c, LV_OPA_COVER);
        g_rect(&g, 8, 1, 3, 12, 1, c, LV_OPA_COVER);
    } else {
        /* Centred by its centroid ((4 + 13 + 4) / 3 = 7), not its bounding box, so it looks centred. */
        t.p[0] = (lv_point_precise_t){ g.x + 4, g.y + 1 };
        t.p[1] = (lv_point_precise_t){ g.x + 13, g.y + 7 };
        t.p[2] = (lv_point_precise_t){ g.x + 4, g.y + 13 };
        lv_draw_triangle(g.L, &t);
    }
}

static void transport_click(lv_event_t *e)
{
    cb_model_t *m = cb_model();
    btn_kind_t k = (btn_kind_t)(intptr_t)lv_event_get_user_data(e);
    if(k == BTN_PLAY) {
        cb_model_set_int(m->media_playing, !lv_subject_get_int(m->media_playing));
        return;
    }
    int32_t t = lv_subject_get_int(m->media_track);
    cb_model_set_int(m->media_track, (t + (k == BTN_NEXT ? 1 : 2)) % 3);
    cb_model_set_int(m->media_pos_s, 0);
}

static void shuffle_click(lv_event_t *e)
{
    LV_UNUSED(e);
    s_shuffle_on = !s_shuffle_on;
    lv_label_set_text(ui_chip_label(s_shuffle), s_shuffle_on ? "Shuffle on" : "Shuffle off");
    lv_obj_set_style_bg_color(lv_obj_get_child(s_shuffle, 0), cb_col(s_shuffle_on ? CB_GREEN : CB_TEXT_MID), 0);
}

static lv_obj_t *round_btn(lv_obj_t *parent, int32_t d, btn_kind_t k)
{
    bool lg = k == BTN_PLAY;
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, d, d);
    lv_obj_set_clickable(b, true);
    lv_obj_set_ext_click_area(b, 5);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, lg ? cb_col(CB_TEXT_HI) : lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b, lg ? LV_OPA_COVER : 20, 0);
    lv_obj_set_style_bg_opa(b, lg ? 200 : 46, LV_STATE_PRESSED);
    lv_obj_add_event_cb(b, glyph_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)k);
    lv_obj_add_event_cb(b, transport_click, LV_EVENT_CLICKED, (void *)(intptr_t)k);
    return b;
}

static void media_card(lv_obj_t *col)
{
    cb_model_t *m = cb_model();
    lv_obj_t *card = ui_card(col);

    lv_obj_t *top = ui_row(card, 12);
    s_art = ui_plain(top);
    lv_obj_set_size(s_art, 46, 46);
    lv_obj_set_style_radius(s_art, 10, 0);
    lv_obj_set_style_bg_opa(s_art, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s_art, cb_col(CB_VIOLET), 0);
    lv_obj_set_style_bg_grad_color(s_art, cb_col(CB_CYAN), 0);
    lv_obj_set_style_bg_grad_dir(s_art, LV_GRAD_DIR_HOR, 0);
    for(int i = 0; i < 4; i++) {
        s_eq[i] = ui_plain(s_art);
        lv_obj_set_pos(s_eq[i], 12 + i * 6, 32);
        lv_obj_set_size(s_eq[i], 3, 6);
        lv_obj_set_style_radius(s_eq[i], 2, 0);
        lv_obj_set_style_bg_color(s_eq[i], cb_col(CB_BASE), 0);
        lv_obj_set_style_bg_opa(s_eq[i], 179, 0);
    }

    lv_obj_t *info = ui_plain(top);
    lv_obj_set_height(info, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(info, 1);
    lv_obj_set_flex_flow(info, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(info, 2, 0);
    s_title = ui_label_line(info, "", CB_F_BODY, CB_TEXT_HI);
    s_msub = ui_label_line(info, "", CB_F_SMALL, CB_TEXT_LO);

    lv_obj_t *tr = box(top, LV_FLEX_FLOW_ROW, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(tr, 10, 0);
    round_btn(tr, 34, BTN_PREV);
    s_play = round_btn(tr, 42, BTN_PLAY);
    round_btn(tr, 34, BTN_NEXT);

    lv_obj_t *bot = ui_row(card, 10);
    s_tnow = ui_label(bot, "", CB_F_MONO_S, CB_TEXT_LO);
    s_prog = lv_bar_create(bot);
    lv_obj_remove_style_all(s_prog);
    lv_obj_set_height(s_prog, 4);
    lv_obj_set_flex_grow(s_prog, 1);
    lv_obj_set_style_bg_color(s_prog, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_prog, 25, LV_PART_MAIN);
    lv_obj_set_style_radius(s_prog, 2, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_prog, cb_col(CB_GREEN), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_prog, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(s_prog, 2, LV_PART_INDICATOR);
    s_tdur = ui_label(bot, "", CB_F_MONO_S, CB_TEXT_LO);
    s_shuffle = ui_chip(bot, "Shuffle off", CB_TEXT_MID, false);
    lv_obj_set_clickable(s_shuffle, true);
    lv_obj_add_event_cb(s_shuffle, shuffle_click, LV_EVENT_CLICKED, NULL);
    s_vol = ui_chip(bot, "Vol 40", CB_CYAN, false);

    lv_subject_add_observer_obj(m->media_track, track_obs, s_title, NULL);
    lv_subject_add_observer_obj(m->media_title_rev, title_obs, s_title, NULL);
    lv_subject_add_observer_obj(m->media_pos_s, pos_obs, s_prog, NULL);
    lv_subject_add_observer_obj(m->media_playing, playing_obs, s_art, NULL);
    lv_subject_add_observer_obj(m->volume, volume_obs, s_vol, NULL);
}

/* Every decorative loop of the column in one ambient tick: fan pulse, tile glow, wiper sweep, EQ bars. */
static void ambient_step(void *ctx, uint32_t now)
{
    LV_UNUSED(ctx);
    for(int32_t i = 0; i < s_fan_on; i++) {   /* 255 <-> 115 over 1.4 s, each bar 180 ms behind the last */
        lv_opa_t o = (lv_opa_t)(LV_OPA_COVER - 140 * cb_ambient_wave(now, 1400, 1400 - (uint32_t)(i * 180) % 1400) / 1000);
        if(lv_obj_get_style_bg_opa(s_fan[i], 0) != o) lv_obj_set_style_bg_opa(s_fan[i], o, 0);
    }

    int32_t glow = LV_OPA_COVER - 115 * cb_ambient_wave(now, 3000, 0) / 1000;
    if(glow != s_glow) {
        s_glow = glow;
        lv_obj_invalidate(s_tiles[T_SEATS].g);
        lv_obj_invalidate(s_tiles[T_LIGHTS].g);
        lv_obj_invalidate(s_tiles[T_AMBIENT].g);
    }

    if(s_wiping) {
        int32_t a = -60 + 120 * cb_ambient_wave(now, 2400, 0) / 1000;
        if(a != s_blade) {
            s_blade = a;
            lv_obj_invalidate(s_tiles[T_WIPERS].g);
        }
    }

    if(s_playing) {
        static const uint32_t phase[4] = { 0, 600, 1200, 300 };
        for(int i = 0; i < 4; i++) eq_set(s_eq[i], 6 + 16 * cb_ambient_wave(now, 1800, phase[i]) / 1000);
    }
}

void comfort_build(lv_obj_t *column) { comfort_build_parts(column, column, column); }

void comfort_build_parts(lv_obj_t *climate, lv_obj_t *tiles, lv_obj_t *media)
{
    s_ready = false;
    climate_card(climate);
    tiles_grid(tiles);
    media_card(media);
    s_ready = true;
    cb_ambient_add(ambient_step, NULL);
}
