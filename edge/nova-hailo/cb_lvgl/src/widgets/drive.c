#include "widgets/drive.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "widgets/sparkline.h"
#include "widgets/ui_kit.h"
#include <math.h>

#define CARD_INNER_W 552   /* column content 580 - card padding 2 x 14 */

static lv_obj_t *s_speed_lbl, *s_spark, *s_gears[4], *s_soc_lbl, *s_soc_bar, *s_range_lbl;
static lv_obj_t *s_map, *s_dot, *s_eta_lbl;
static int32_t s_shown_x100;

/* ---------- speed card ---------- */

static void ease_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    int32_t target = lv_subject_get_int(cb_model()->speed_kph) * 100;
    int32_t before = (s_shown_x100 + 50) / 100;
    s_shown_x100 += (target - s_shown_x100) * 6 / 100;
    if(LV_ABS(target - s_shown_x100) < 50) s_shown_x100 = target;
    int32_t after = (s_shown_x100 + 50) / 100;
    if(after != before) lv_label_set_text_fmt(s_speed_lbl, "%d", (int)after);
}

static void speed_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    sparkline_push(s_spark, lv_subject_get_int(subj));
}

static void gear_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    int32_t g = lv_subject_get_int(subj);
    for(int i = 0; i < 4; i++) lv_obj_set_style_text_color(s_gears[i], cb_col(i == g ? CB_CYAN : CB_TEXT_LO), 0);
}

static void soc_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    int32_t v = lv_subject_get_int(subj);
    lv_label_set_text_fmt(s_soc_lbl, "%d%%", (int)v);
    lv_bar_set_value(s_soc_bar, v, LV_ANIM_ON);
}

static void range_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    lv_label_set_text_fmt(s_range_lbl, "%d km", (int)lv_subject_get_int(subj));
}

static void speed_card(lv_obj_t *col)
{
    lv_obj_t *card = ui_card(col);

    lv_obj_t *top = ui_row(card, 10);
    lv_obj_set_flex_align(top, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    s_speed_lbl = ui_label(top, "62", CB_F_SPEED, CB_TEXT_HI);
    s_shown_x100 = lv_subject_get_int(cb_model()->speed_kph) * 100;
    lv_label_set_text_fmt(s_speed_lbl, "%d", (int)(s_shown_x100 / 100));
    lv_obj_t *kmh = ui_label(top, "km/h", CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_style_pad_bottom(kmh, 9, 0);
    ui_spacer(top);
    static const char *gears[4] = { "P", "R", "N", "D" };
    for(int i = 0; i < 4; i++) {
        s_gears[i] = ui_label(top, gears[i], CB_F_VALUE, CB_TEXT_LO);
        lv_obj_set_style_pad_bottom(s_gears[i], 9, 0);
    }
    lv_subject_add_observer_obj(cb_model()->gear, gear_obs, s_gears[0], NULL);
    lv_timer_create(ease_cb, 33, NULL);

    s_spark = sparkline_create(card, CARD_INNER_W, 34, 40, CB_CYAN, 40, 85);
    lv_subject_add_observer_obj(cb_model()->speed_kph, speed_obs, s_spark, NULL);

    lv_obj_t *soc = ui_row(card, 10);
    s_soc_lbl = ui_label(soc, "68%", CB_F_MONO_L, CB_TEXT_HI);
    s_soc_bar = lv_bar_create(soc);
    lv_obj_remove_style_all(s_soc_bar);
    lv_obj_set_height(s_soc_bar, 5);
    lv_obj_set_flex_grow(s_soc_bar, 1);
    lv_obj_set_style_bg_color(s_soc_bar, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_soc_bar, 25, LV_PART_MAIN);
    lv_obj_set_style_radius(s_soc_bar, 3, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_soc_bar, cb_col(CB_GREEN), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_soc_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(s_soc_bar, 3, LV_PART_INDICATOR);
    lv_obj_set_style_anim_duration(s_soc_bar, 800, 0);
    lv_bar_set_range(s_soc_bar, 0, 100);
    s_range_lbl = ui_label(soc, "210 km", CB_F_MONO, CB_TEXT_MID);
    lv_subject_add_observer_obj(cb_model()->soc_pct, soc_obs, s_soc_lbl, NULL);
    lv_subject_add_observer_obj(cb_model()->range_km, range_obs, s_range_lbl, NULL);

    lv_obj_t *chips = ui_row(card, 6);
    ui_chip(chips, "Locked", CB_GREEN, false);
    ui_chip(chips, "Lights auto", CB_CYAN, false);
    ui_chip(chips, "Eco", CB_TEXT_MID, false);
}

/* ---------- mini map ---------- */

/* Route home in a 0..1000 box: bottom-left to top-right with a gentle S. */
static const int16_t k_route[][2] = {
    { 70, 880 }, { 160, 820 }, { 250, 745 }, { 330, 695 }, { 400, 665 }, { 470, 650 }, { 540, 620 },
    { 600, 560 }, { 650, 480 }, { 700, 400 }, { 760, 330 }, { 820, 270 }, { 880, 230 }, { 930, 200 },
};
#define ROUTE_N ((int32_t)(sizeof(k_route) / sizeof(k_route[0])))
static lv_point_precise_t s_pts[ROUTE_N];

static void route_points(int32_t x0, int32_t y0, int32_t w, int32_t h)
{
    for(int32_t i = 0; i < ROUTE_N; i++) {
        s_pts[i].x = x0 + k_route[i][0] * w / 1000;
        s_pts[i].y = y0 + k_route[i][1] * h / 1000;
    }
}

static void map_draw(lv_event_t *e)
{
    lv_obj_t *map = lv_event_get_target_obj(e);
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_area_t a;
    lv_obj_get_coords(map, &a);

    lv_draw_line_dsc_t ld;
    lv_draw_line_dsc_init(&ld);
    ld.color = lv_color_white();
    ld.opa = 9;
    ld.width = 1;
    for(int32_t x = a.x1 + 40; x < a.x2; x += 40) {
        ld.p1 = (lv_point_precise_t){ x, a.y1 + 1 };
        ld.p2 = (lv_point_precise_t){ x, a.y2 - 1 };
        lv_draw_line(layer, &ld);
    }
    for(int32_t y = a.y1 + 40; y < a.y2; y += 40) {
        ld.p1 = (lv_point_precise_t){ a.x1 + 1, y };
        ld.p2 = (lv_point_precise_t){ a.x2 - 1, y };
        lv_draw_line(layer, &ld);
    }

    route_points(a.x1, a.y1, lv_area_get_width(&a), lv_area_get_height(&a));
    ld.points = s_pts;
    ld.point_cnt = ROUTE_N;
    ld.round_start = 1;
    ld.round_end = 1;
    ld.width = 14;
    ld.color = lv_color_mix(lv_color_white(), lv_color_hex(0x0b0f15), 20);   /* opaque: no blobs at joints */
    ld.opa = LV_OPA_COVER;
    lv_draw_line(layer, &ld);                 /* road */
    ld.width = 4;
    ld.color = cb_col(CB_CYAN);
    ld.opa = LV_OPA_COVER;
    lv_draw_line(layer, &ld);                 /* route */

    lv_draw_rect_dsc_t rd;
    lv_draw_rect_dsc_init(&rd);
    rd.bg_color = lv_color_white();
    rd.bg_opa = LV_OPA_COVER;
    rd.radius = LV_RADIUS_CIRCLE;
    int32_t dx = (int32_t)s_pts[ROUTE_N - 1].x, dy = (int32_t)s_pts[ROUTE_N - 1].y;
    lv_area_t dest = { dx - 7, dy - 7, dx + 7, dy + 7 };
    lv_draw_rect(layer, &rd, &dest);          /* destination */
}

/* Put the position dot at `permille` of the route's length. */
static void place_dot(void)
{
    int32_t w = lv_obj_get_width(s_map), h = lv_obj_get_height(s_map);
    if(w <= 0 || h <= 0) return;
    route_points(0, 0, w, h);
    float seg[ROUTE_N], total = 0.0f;
    for(int32_t i = 0; i + 1 < ROUTE_N; i++) {
        float dx = (float)(s_pts[i + 1].x - s_pts[i].x), dy = (float)(s_pts[i + 1].y - s_pts[i].y);
        seg[i] = sqrtf(dx * dx + dy * dy);
        total += seg[i];
    }
    float want = total * (float)lv_subject_get_int(cb_model()->route_permille) / 1000.0f;
    int32_t i = 0;
    while(i + 2 < ROUTE_N && want > seg[i]) {
        want -= seg[i];
        i++;
    }
    float f = seg[i] > 0.0f ? want / seg[i] : 0.0f;
    if(f > 1.0f) f = 1.0f;
    int32_t x = (int32_t)(s_pts[i].x + (s_pts[i + 1].x - s_pts[i].x) * f);
    int32_t y = (int32_t)(s_pts[i].y + (s_pts[i + 1].y - s_pts[i].y) * f);
    lv_obj_set_pos(s_dot, x - 8, y - 8);
}

static void route_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    LV_UNUSED(subj);
    place_dot();
}

static void map_resized(lv_event_t *e)
{
    LV_UNUSED(e);
    place_dot();
}

static void eta_obs(lv_observer_t *o, lv_subject_t *subj)
{
    LV_UNUSED(o);
    int32_t eta = lv_subject_get_int(subj);
    lv_label_set_text_fmt(s_eta_lbl, "%d min \xC2\xB7 %d km", (int)eta, (int)(eta * 24 / 35));
}

static void mini_map(lv_obj_t *col)
{
    s_map = ui_plain(col);
    lv_obj_set_width(s_map, lv_pct(100));
    lv_obj_set_flex_grow(s_map, 1);
    lv_obj_set_style_bg_color(s_map, lv_color_hex(0x0b0f15), 0);
    lv_obj_set_style_bg_opa(s_map, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(s_map, 14, 0);
    lv_obj_add_event_cb(s_map, map_draw, LV_EVENT_DRAW_MAIN_END, NULL);
    lv_obj_add_event_cb(s_map, map_resized, LV_EVENT_SIZE_CHANGED, NULL);

    s_dot = ui_plain(s_map);
    lv_obj_set_size(s_dot, 16, 16);
    lv_obj_set_style_radius(s_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_dot, cb_col(CB_CYAN), 0);
    lv_obj_set_style_bg_opa(s_dot, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(s_dot, cb_col(CB_BASE), 0);
    lv_obj_set_style_border_width(s_dot, 3, 0);

    lv_obj_t *tag = ui_row(s_map, 8);
    lv_obj_set_width(tag, LV_SIZE_CONTENT);
    lv_obj_set_pos(tag, 12, 12);
    lv_obj_set_style_bg_color(tag, lv_color_hex(0x14181f), 0);
    lv_obj_set_style_bg_opa(tag, 224, 0);
    lv_obj_set_style_border_color(tag, lv_color_white(), 0);
    lv_obj_set_style_border_opa(tag, 23, 0);
    lv_obj_set_style_border_width(tag, 1, 0);
    lv_obj_set_style_radius(tag, 10, 0);
    lv_obj_set_style_pad_hor(tag, 11, 0);
    lv_obj_set_style_pad_ver(tag, 6, 0);
    ui_label(tag, "Home", CB_F_BODY, CB_TEXT_HI);
    s_eta_lbl = ui_label(tag, "", CB_F_MONO, CB_TEXT_MID);
    lv_subject_add_observer_obj(cb_model()->eta_min, eta_obs, s_eta_lbl, NULL);
    lv_subject_add_observer_obj(cb_model()->route_permille, route_obs, s_dot, NULL);
}

void drive_build(lv_obj_t *column)
{
    speed_card(column);
    mini_map(column);
}

void drive_build_speed(lv_obj_t *parent) { speed_card(parent); }
