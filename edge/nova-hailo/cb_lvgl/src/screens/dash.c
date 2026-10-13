#include "screens/dash.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "widgets/ui_kit.h"
#include <stdio.h>
#include <time.h>

#define SIDE 20
#define STRIP_Y 14
#define STRIP_H 44
#define COL_Y 70
#define COL_H 560
#define COL_W 616
#define COL_GAP 16
#define DOCK_Y 636
#define DOCK_H 72
#define BAR_SLOT_W 800   /* bar is 760; room for the expand spring's overshoot */

static lv_obj_t *s_clock;

typedef enum { ICON_WIFI, ICON_BT } icon_t;

/* Status icons drawn in place: Wi-Fi = two arcs over a dot (20x14), Bluetooth = the rune as one polyline (12x16). */
static void icon_draw(lv_event_t *e)
{
    lv_obj_t *o = lv_event_get_target_obj(e);
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    lv_color_t c = cb_col(CB_TEXT_2);

    if((icon_t)(intptr_t)lv_event_get_user_data(e) == ICON_WIFI) {
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.center = (lv_point_t){ a.x1 + 10, a.y1 + 13 };
        arc.start_angle = 225;
        arc.end_angle = 315;
        arc.width = 2;
        arc.rounded = 1;
        arc.color = c;
        arc.opa = LV_OPA_COVER;
        arc.radius = 13;
        lv_draw_arc(layer, &arc);
        arc.radius = 8;
        lv_draw_arc(layer, &arc);
        lv_draw_rect_dsc_t dot;
        lv_draw_rect_dsc_init(&dot);
        dot.bg_color = c;
        dot.bg_opa = LV_OPA_COVER;
        dot.radius = LV_RADIUS_CIRCLE;
        lv_area_t d = { a.x1 + 8, a.y1 + 10, a.x1 + 11, a.y1 + 13 };
        lv_draw_rect(layer, &dot, &d);
    } else {
        static const float rune[][2] = { { 2, 5 }, { 10, 11 }, { 6, 14.5f }, { 6, 1.5f }, { 10, 5 }, { 2, 11 } };
        lv_point_precise_t p[6];
        for(int i = 0; i < 6; i++) p[i] = (lv_point_precise_t){ a.x1 + rune[i][0], a.y1 + rune[i][1] };
        lv_draw_line_dsc_t ln;
        lv_draw_line_dsc_init(&ln);
        ln.points = p;
        ln.point_cnt = 6;
        ln.width = 2;
        ln.color = c;
        ln.opa = LV_OPA_COVER;
        ln.round_start = 1;
        ln.round_end = 1;
        lv_draw_line(layer, &ln);
    }
}

static void status_icon(lv_obj_t *parent, icon_t icon)
{
    lv_obj_t *o = ui_plain(parent);
    lv_obj_set_size(o, icon == ICON_WIFI ? 20 : 12, icon == ICON_WIFI ? 14 : 16);
    lv_obj_add_event_cb(o, icon_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)icon);
}

static void clock_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    time_t now = time(NULL);
    struct tm tmv;
    localtime_r(&now, &tmv);
    int h = tmv.tm_hour % 12;
    char b[16];
    snprintf(b, sizeof b, "%d:%02d %s", h ? h : 12, tmv.tm_min, tmv.tm_hour >= 12 ? "PM" : "AM");
    lv_label_set_text(s_clock, b);
}

static void session_cb(lv_observer_t *o, lv_subject_t *s)
{
    int32_t v = lv_subject_get_int(s);
    lv_label_set_text_fmt(lv_observer_get_target_obj(o), "Connected \xC2\xB7 %d:%02d", (int)(v / 60), (int)(v % 60));
}

static void strip(lv_obj_t *panel)
{
    lv_obj_t *row = ui_row(panel, 14);
    lv_obj_set_size(row, CB_PANEL_W - 2 * SIDE, STRIP_H);
    lv_obj_set_pos(row, SIDE, STRIP_Y);

    lv_obj_t *avatar = ui_plain(row);
    lv_obj_set_size(avatar, 34, 34);
    lv_obj_set_style_radius(avatar, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(avatar, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(avatar, 15, 0);
    lv_obj_set_style_border_color(avatar, lv_color_white(), 0);
    lv_obj_set_style_border_opa(avatar, 23, 0);
    lv_obj_set_style_border_width(avatar, 1, 0);
    lv_obj_center(ui_label(avatar, "SK", CB_F_LABEL, CB_TEXT_2));

    lv_obj_t *brand = ui_label(row, "NOVA", CB_F_VALUE, CB_TEXT_HI);
    lv_obj_set_style_text_letter_space(brand, 6, 0);
    ui_label(row, "on-device \xC2\xB7 Pi 5 + Hailo", CB_F_LABEL, CB_TEXT_LO);
    ui_spacer(row);

    s_clock = ui_label(row, "", CB_F_MONO_L, CB_TEXT_HI);
    clock_cb(NULL);
    lv_timer_create(clock_cb, 1000, NULL);
    lv_obj_t *sess = ui_chip(row, "Connected", CB_GREEN, true);
    lv_subject_add_observer_obj(cb_model()->session_s, session_cb, ui_chip_label(sess), NULL);
    ui_spacer(row);

    lv_obj_t *net = ui_row(row, 10);
    lv_obj_set_width(net, LV_SIZE_CONTENT);
    lv_obj_t *sig = ui_plain(net);
    lv_obj_set_size(sig, LV_SIZE_CONTENT, 13);
    lv_obj_set_flex_flow(sig, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(sig, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_set_style_pad_column(sig, 2, 0);
    static const int32_t heights[4] = { 4, 7, 10, 13 };
    for(int i = 0; i < 4; i++) {
        lv_obj_t *b = ui_plain(sig);
        lv_obj_set_size(b, 3, heights[i]);
        lv_obj_set_style_radius(b, 1, 0);
        lv_obj_set_style_bg_color(b, cb_col(CB_TEXT_HI), 0);
        lv_obj_set_style_bg_opa(b, i == 3 ? 77 : LV_OPA_COVER, 0);
    }
    ui_label(net, "5G", CB_F_LABEL, CB_TEXT_MID);
    status_icon(net, ICON_WIFI);
    status_icon(net, ICON_BT);
    ui_label(row, "20\xC2\xB0" "C", CB_F_BODY, CB_TEXT_HI);
}

static lv_obj_t *column(lv_obj_t *panel, int idx, const char *title)
{
    lv_obj_t *p = ui_panel(panel, title);
    lv_obj_set_size(p, COL_W, COL_H);
    lv_obj_set_pos(p, SIDE + idx * (COL_W + COL_GAP), COL_Y);
    return p;
}

static lv_obj_t *frame_button(lv_obj_t *parent, const char *letter, bool active)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, 28, 26);
    lv_obj_set_style_radius(b, 8, 0);
    lv_obj_set_style_bg_color(b, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b, 11, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_set_style_border_color(b, active ? cb_col(CB_CYAN) : lv_color_white(), 0);
    lv_obj_set_style_border_opa(b, active ? 140 : 23, 0);
    lv_obj_center(ui_label(b, letter, CB_F_MONO, active ? CB_TEXT_HI : CB_TEXT_LO));
    return b;
}

static lv_obj_t *dock(lv_obj_t *panel, dash_t *d)
{
    lv_obj_t *left = ui_row(panel, 8);
    lv_obj_set_size(left, LV_SIZE_CONTENT, DOCK_H);
    lv_obj_set_pos(left, SIDE, DOCK_Y);
    ui_chip(left, "Settings", CB_TEXT_MID, false);
    ui_chip(left, "Metrics", CB_CYAN, false);
    ui_chip(left, "More", CB_TEXT_MID, false);

    d->dock_mid = ui_plain(panel);
    lv_obj_set_size(d->dock_mid, BAR_SLOT_W, DOCK_H);
    lv_obj_set_pos(d->dock_mid, (CB_PANEL_W - BAR_SLOT_W) / 2, DOCK_Y);
    lv_obj_set_overflow_visible(d->dock_mid, true);

    lv_obj_t *right = ui_col(panel, 6);
    lv_obj_set_size(right, LV_SIZE_CONTENT, DOCK_H);
    lv_obj_set_flex_align(right, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_align(right, LV_ALIGN_TOP_RIGHT, -SIDE, DOCK_Y);
    lv_obj_t *frames = ui_row(right, 4);
    lv_obj_set_width(frames, LV_SIZE_CONTENT);
    frame_button(frames, "D", true);
    frame_button(frames, "E", false);
    frame_button(frames, "F", false);
    frame_button(frames, "G", false);
    frame_button(frames, "H", false);
    d->pause_btn = ui_chip(frames, "Pause demo", CB_TEXT_MID, false);
    lv_obj_set_clickable(d->pause_btn, true);
    ui_label(right, "Prototype \xC2\xB7 simulated \xC2\xB7 E-H later", CB_F_SMALL, CB_TEXT_LO);
    return left;
}

dash_t dash_create(lv_obj_t *panel)
{
    dash_t d = { 0 };
    strip(panel);
    d.drive = column(panel, 0, "DRIVE");
    d.comfort = column(panel, 1, "COMFORT");
    d.connected = column(panel, 2, "CONNECTED");
    dock(panel, &d);
    return d;
}
