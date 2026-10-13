#include "screens/cockpit.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "map/map_view.h"
#include "widgets/comfort.h"
#include "widgets/connected.h"
#include "widgets/drive.h"
#include "widgets/settings_sheet.h"
#include "widgets/ui_kit.h"
#include <math.h>
#include <stdio.h>
#include <time.h>

#define SIDE 32
#define LEFT_W 560
#define GAP 24
#define BAR_SLOT_W 800

static cockpit_t s_ck;
static cockpit_hooks_t s_hooks;
static lv_obj_t *s_tv, *s_pager_btn[3], *s_clock;
static lv_obj_t *s_nav_icon, *s_nav_turn, *s_nav_instr, *s_nav_dest, *s_nav_via, *s_nav_trip, *s_nav_bar;
static nav_maneuver_t s_nav_kind = NAV_M_STRAIGHT;
static lv_obj_t *s_compass_btn;
static double s_bearing;

cockpit_t *cockpit(void) { return &s_ck; }

void cockpit_set_chip(lv_obj_t *chip, const char *text, lv_color_t dot)
{
    lv_label_set_text(ui_chip_label(chip), text);
    lv_obj_t *d = lv_obj_get_child(chip, 0);
    lv_obj_set_style_bg_color(d, dot, 0);
    lv_obj_set_style_outline_color(d, dot, 0);
}

static lv_obj_t *glass(lv_obj_t *parent, int32_t radius)
{
    lv_obj_t *o = ui_plain(parent);
    lv_obj_set_style_bg_color(o, lv_color_hex(0x0e1117), 0);
    lv_obj_set_style_bg_opa(o, 225, 0);
    lv_obj_set_style_border_color(o, lv_color_white(), 0);
    lv_obj_set_style_border_opa(o, 23, 0);
    lv_obj_set_style_border_width(o, 1, 0);
    lv_obj_set_style_radius(o, radius, 0);
    return o;
}

/* ---------- status strip ---------- */

static void clock_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    time_t now = time(NULL);
    struct tm tmv;
    localtime_r(&now, &tmv);
    lv_label_set_text_fmt(s_clock, "%02d:%02d", tmv.tm_hour, tmv.tm_min);
}

static void socline_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(s);
    cb_model_t *m = cb_model();
    lv_label_set_text_fmt(lv_observer_get_target_obj(o), "%d%% \xC2\xB7 %d km", (int)lv_subject_get_int(m->soc_pct),
                          (int)lv_subject_get_int(m->range_km));
}

static void llm_click(lv_event_t *e)
{
    LV_UNUSED(e);
    settings_sheet_open();
}

static void status_strip(lv_obj_t *panel, int32_t w, int32_t h)
{
    lv_obj_t *row = ui_row(panel, 22);
    lv_obj_set_size(row, w - 2 * 36, h);
    lv_obj_set_pos(row, 36, 0);
    s_clock = ui_label(row, "--:--", CB_F_TITLE, CB_TEXT_HI);
    clock_cb(NULL);
    lv_timer_create(clock_cb, 1000, NULL);
    s_ck.place = ui_label(row, "Palo Alto", CB_F_BODY, CB_TEXT_MID);
    ui_spacer(row);
    s_ck.link_chip = ui_chip(row, "Simulated cabin", CB_YELLOW, true);
    lv_obj_t *llm = ui_chip(row, "Model \xC2\xB7 tap to choose", CB_VIOLET, false);
    lv_obj_set_clickable(llm, true);
    lv_obj_set_ext_click_area(llm, 8);
    lv_obj_add_event_cb(llm, llm_click, LV_EVENT_CLICKED, NULL);
    settings_sheet_init(llm);
    ui_label(row, "Nova on-device \xC2\xB7 Pi 5 + Hailo", CB_F_LABEL, CB_TEXT_LO);
    ui_spacer(row);
    ui_label(row, "5G", CB_F_LABEL, CB_TEXT_MID);
    lv_obj_t *soc = ui_label(row, "", CB_F_BODY, CB_TEXT_2);
    lv_subject_add_observer_obj(cb_model()->soc_pct, socline_obs, soc, NULL);
    lv_subject_add_observer_obj(cb_model()->range_km, socline_obs, soc, NULL);
}

/* ---------- right side: pages ---------- */

static void pager_sync(int idx)
{
    for(int i = 0; i < 3; i++) {
        bool on = i == idx;
        lv_obj_set_style_bg_opa(s_pager_btn[i], on ? 30 : 0, 0);
        lv_obj_set_style_text_color(lv_obj_get_child(s_pager_btn[i], 0), cb_col(on ? CB_TEXT_HI : CB_TEXT_LO), 0);
    }
    map_view_set_active(s_ck.map, idx == 0);
}

static void tile_changed(lv_event_t *e)
{
    lv_obj_t *tile = lv_tileview_get_tile_active(lv_event_get_target_obj(e));
    pager_sync((int)(lv_obj_get_x(tile) / LV_MAX(1, lv_obj_get_width(tile))));
}

void cockpit_show_page(int idx)
{
    lv_tileview_set_tile_by_index(s_tv, (uint32_t)idx, 0, cb_reduce_motion() ? LV_ANIM_OFF : LV_ANIM_ON);
    pager_sync(idx);
}

static void pager_click(lv_event_t *e) { cockpit_show_page((int)(intptr_t)lv_event_get_user_data(e)); }

/* A crescent, rendered once per pixel with analytic anti-aliasing (coverage of the lit disc times what the shadow
   disc up and to the right leaves of it) and drawn as an image: no seams, horns that taper to points. */
#define MOON_PX 28
static uint32_t s_moon_px[MOON_PX * MOON_PX];
static lv_image_dsc_t s_moon;

static float cover(float signed_dist) { return signed_dist < -0.5f ? 1.0f : signed_dist > 0.5f ? 0.0f : 0.5f - signed_dist; }

static void draw_moon(lv_layer_t *layer, float cx, float cy, lv_color_t c)
{
    if(!s_moon.data) {
        const float R = 10.0f, r = 8.0f, ox = 5.0f, oy = -4.0f, mx = MOON_PX / 2.0f, my = MOON_PX / 2.0f + 1;
        for(int y = 0; y < MOON_PX; y++)
            for(int x = 0; x < MOON_PX; x++) {
                float fx = x + 0.5f - mx, fy = y + 0.5f - my;
                float lit = cover(sqrtf(fx * fx + fy * fy) - R);
                float shade = cover(sqrtf((fx - ox) * (fx - ox) + (fy - oy) * (fy - oy)) - r);
                uint32_t alpha = (uint32_t)(lit * (1.0f - shade) * 255.0f + 0.5f);
                s_moon_px[y * MOON_PX + x] = alpha << 24 | (uint32_t)c.red << 16 | (uint32_t)c.green << 8 | c.blue;
            }
        s_moon.header.magic = LV_IMAGE_HEADER_MAGIC;
        s_moon.header.cf = LV_COLOR_FORMAT_ARGB8888;
        s_moon.header.w = MOON_PX;
        s_moon.header.h = MOON_PX;
        s_moon.header.stride = MOON_PX * 4;
        s_moon.data_size = sizeof s_moon_px;
        s_moon.data = (const uint8_t *)s_moon_px;
    }
    lv_draw_image_dsc_t d;
    lv_draw_image_dsc_init(&d);
    d.src = &s_moon;
    int32_t x0 = (int32_t)(cx - MOON_PX / 2.0f), y0 = (int32_t)(cy - MOON_PX / 2.0f);
    lv_area_t area = { x0, y0, x0 + MOON_PX - 1, y0 + MOON_PX - 1 };
    lv_draw_image(layer, &d, &area);
}

/* Icons our fonts lack, drawn in place: the recenter target and the play / pause glyph. */
typedef enum { ICON_RECENTER, ICON_MEDIA, ICON_THEME, ICON_COMPASS, ICON_OVERVIEW } icon_t;

static void tri(lv_layer_t *layer, lv_color_t c, lv_opa_t opa, float x0, float y0, float x1, float y1, float x2, float y2)
{
    lv_draw_triangle_dsc_t t;
    lv_draw_triangle_dsc_init(&t);
    t.color = c;
    t.opa = opa;
    t.p[0] = (lv_point_precise_t){ x0, y0 };
    t.p[1] = (lv_point_precise_t){ x1, y1 };
    t.p[2] = (lv_point_precise_t){ x2, y2 };
    lv_draw_triangle(layer, &t);
}

/* The compass needle points to north on screen (red), whatever the map's bearing. */
static void draw_compass(lv_layer_t *layer, float cx, float cy)
{
    float r = (float)(-s_bearing * M_PI / 180.0), dx = sinf(r), dy = -cosf(r), px = cosf(r), py = sinf(r);
    tri(layer, lv_color_hex(0xe8553d), LV_OPA_COVER, cx + 15 * dx, cy + 15 * dy, cx + 5 * px, cy + 5 * py, cx - 5 * px, cy - 5 * py);
    tri(layer, cb_col(CB_TEXT_HI), 180, cx - 15 * dx, cy - 15 * dy, cx + 5 * px, cy + 5 * py, cx - 5 * px, cy - 5 * py);
}

/* "Whole route": four corner brackets. */
static void draw_overview(lv_layer_t *layer, float cx, float cy, lv_color_t c)
{
    lv_draw_line_dsc_t ln;
    lv_draw_line_dsc_init(&ln);
    ln.color = c;
    ln.width = 3;
    ln.round_start = ln.round_end = 1;
    for(int i = 0; i < 4; i++) {
        float sx = i & 1 ? 1.0f : -1.0f, sy = i & 2 ? 1.0f : -1.0f;
        ln.p1 = (lv_point_precise_t){ cx + sx * 12, cy + sy * 5 };
        ln.p2 = (lv_point_precise_t){ cx + sx * 12, cy + sy * 12 };
        lv_draw_line(layer, &ln);
        ln.p1 = (lv_point_precise_t){ cx + sx * 5, cy + sy * 12 };
        lv_draw_line(layer, &ln);
    }
}

static void icon_draw(lv_event_t *e)
{
    lv_obj_t *o = lv_event_get_target_obj(e);
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    int32_t cx = a.x1 + lv_area_get_width(&a) / 2, cy = a.y1 + lv_area_get_height(&a) / 2;
    lv_color_t c = cb_col(CB_TEXT_HI);
    if((icon_t)(intptr_t)lv_event_get_user_data(e) == ICON_COMPASS) {
        draw_compass(layer, (float)cx, (float)cy);
        return;
    }
    if((icon_t)(intptr_t)lv_event_get_user_data(e) == ICON_OVERVIEW) {
        draw_overview(layer, (float)cx, (float)cy, c);
        return;
    }
    if((icon_t)(intptr_t)lv_event_get_user_data(e) == ICON_THEME) {   /* the mode a tap switches to: sun or moon */
        bool night = map_view_night(s_ck.map);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.center = (lv_point_t){ cx, cy };
        arc.color = c;
        if(night) {
            arc.radius = 6;
            arc.width = 6;
            arc.start_angle = 0;
            arc.end_angle = 360;
            lv_draw_arc(layer, &arc);
            lv_draw_line_dsc_t ln;
            lv_draw_line_dsc_init(&ln);
            ln.color = c;
            ln.width = 2;
            ln.round_start = ln.round_end = 1;
            for(int i = 0; i < 8; i++) {
                float an = i * 0.785398f, ca = cosf(an), sa = sinf(an);
                ln.p1 = (lv_point_precise_t){ cx + ca * 9.5f, cy + sa * 9.5f };
                ln.p2 = (lv_point_precise_t){ cx + ca * 13.0f, cy + sa * 13.0f };
                lv_draw_line(layer, &ln);
            }
        }
        else draw_moon(layer, (float)cx, (float)cy - 1, c);
        return;
    }
    if((icon_t)(intptr_t)lv_event_get_user_data(e) == ICON_RECENTER) {
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.center = (lv_point_t){ cx, cy };
        arc.radius = 9;
        arc.width = 2;
        arc.start_angle = 0;
        arc.end_angle = 360;
        arc.color = c;
        lv_draw_arc(layer, &arc);
        lv_draw_rect_dsc_t r;
        lv_draw_rect_dsc_init(&r);
        r.bg_color = c;
        r.radius = LV_RADIUS_CIRCLE;
        lv_area_t dot = { cx - 3, cy - 3, cx + 3, cy + 3 };
        lv_draw_rect(layer, &r, &dot);
        for(int i = 0; i < 4; i++) {   /* the four ticks of a target */
            int32_t dx = i == 0 ? 1 : i == 1 ? -1 : 0, dy = i == 2 ? 1 : i == 3 ? -1 : 0;
            lv_area_t t = { cx + dx * 11 - (dx ? 0 : 1), cy + dy * 11 - (dy ? 0 : 1), cx + dx * 14 + (dx ? 0 : 1),
                            cy + dy * 14 + (dy ? 0 : 1) };
            lv_area_t n = { LV_MIN(t.x1, t.x2), LV_MIN(t.y1, t.y2), LV_MAX(t.x1, t.x2), LV_MAX(t.y1, t.y2) };
            r.radius = 0;
            lv_draw_rect(layer, &r, &n);
        }
        return;
    }
    lv_draw_rect_dsc_t r;
    lv_draw_rect_dsc_init(&r);
    r.bg_color = c;
    r.radius = 1;
    if(lv_subject_get_int(cb_model()->media_playing)) {
        lv_area_t b1 = { cx - 6, cy - 8, cx - 2, cy + 8 }, b2 = { cx + 2, cy - 8, cx + 6, cy + 8 };
        lv_draw_rect(layer, &r, &b1);
        lv_draw_rect(layer, &r, &b2);
    }
    else {
        lv_draw_triangle_dsc_t t;
        lv_draw_triangle_dsc_init(&t);
        t.color = c;
        t.p[0] = (lv_point_precise_t){ cx - 5, cy - 9 };
        t.p[1] = (lv_point_precise_t){ cx + 9, cy };
        t.p[2] = (lv_point_precise_t){ cx - 5, cy + 9 };
        lv_draw_triangle(layer, &t);
    }
}

static lv_obj_t *s_theme_btn;

static void theme_click(lv_event_t *e)
{
    LV_UNUSED(e);
    map_view_set_night(s_ck.map, !map_view_night(s_ck.map));
    lv_obj_invalidate(s_theme_btn);
}

static lv_obj_t *round_btn(lv_obj_t *parent, const char *sym, int32_t d, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = glass(parent, 16);
    lv_obj_set_size(b, d, d);
    lv_obj_set_clickable(b, true);
    lv_obj_set_style_bg_opa(b, 60, LV_STATE_PRESSED);
    if(sym) lv_obj_center(ui_label(b, sym, CB_F_TEMP, CB_TEXT_2));
    else lv_obj_add_event_cb(b, icon_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)ICON_RECENTER);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    return b;
}

static void zoom_click(lv_event_t *e) { map_view_zoom(s_ck.map, lv_event_get_user_data(e) ? 2.0 : 0.5); }
static void recenter_click(lv_event_t *e) { LV_UNUSED(e); map_view_recenter(s_ck.map); }
static void compass_click(lv_event_t *e) { LV_UNUSED(e); map_view_north_up(s_ck.map); }
static void overview_click(lv_event_t *e) { LV_UNUSED(e); map_view_overview(s_ck.map); }

static lv_obj_t *icon_btn(lv_obj_t *parent, icon_t icon, lv_event_cb_t cb)
{
    lv_obj_t *b = glass(parent, 16);
    lv_obj_set_size(b, 56, 56);
    lv_obj_set_clickable(b, true);
    lv_obj_set_style_bg_opa(b, 60, LV_STATE_PRESSED);
    lv_obj_add_event_cb(b, icon_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)icon);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);
    return b;
}

static void compass_poll(lv_timer_t *t)   /* redraw the needle only when the map turned */
{
    LV_UNUSED(t);
    double b = map_view_bearing(s_ck.map);
    if(fabs(b - s_bearing) < 0.5) return;
    s_bearing = b;
    lv_obj_invalidate(s_compass_btn);
}

/* The next maneuver as an arrow: a shaft from below, bent by the turn's angle, or a U-turn, a roundabout ring,
   or a target for the arrival. */
static void maneuver_draw(lv_event_t *e)
{
    lv_obj_t *o = lv_event_get_target_obj(e);
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    float cx = (float)(a.x1 + a.x2) / 2.0f, cy = (float)(a.y1 + a.y2) / 2.0f;
    lv_color_t c = cb_col(CB_TEXT_HI);
    lv_draw_line_dsc_t ln;
    lv_draw_line_dsc_init(&ln);
    ln.color = c;
    ln.width = 7;
    ln.round_start = ln.round_end = 1;
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.color = c;
    arc.width = 6;
    if(s_nav_kind == NAV_M_ARRIVE) {
        arc.center = (lv_point_t){ (int32_t)cx, (int32_t)cy };
        arc.radius = 16;
        arc.start_angle = 0;
        arc.end_angle = 360;
        lv_draw_arc(layer, &arc);
        lv_draw_rect_dsc_t r;
        lv_draw_rect_dsc_init(&r);
        r.bg_color = cb_col(CB_CYAN);
        r.radius = LV_RADIUS_CIRCLE;
        lv_area_t dot = { (int32_t)cx - 6, (int32_t)cy - 6, (int32_t)cx + 6, (int32_t)cy + 6 };
        lv_draw_rect(layer, &r, &dot);
        return;
    }
    if(s_nav_kind == NAV_M_UTURN) {
        ln.p1 = (lv_point_precise_t){ cx + 9, cy + 22 };
        ln.p2 = (lv_point_precise_t){ cx + 9, cy - 6 };
        lv_draw_line(layer, &ln);
        arc.center = (lv_point_t){ (int32_t)cx, (int32_t)cy - 6 };
        arc.radius = 12;
        arc.start_angle = 180;
        arc.end_angle = 360;
        arc.width = 7;
        lv_draw_arc(layer, &arc);
        ln.p1 = (lv_point_precise_t){ cx - 9, cy - 6 };
        ln.p2 = (lv_point_precise_t){ cx - 9, cy + 6 };
        lv_draw_line(layer, &ln);
        tri(layer, c, LV_OPA_COVER, cx - 9, cy + 20, cx - 17, cy + 6, cx - 1, cy + 6);
        return;
    }
    if(s_nav_kind == NAV_M_ROUNDABOUT) {
        arc.center = (lv_point_t){ (int32_t)cx, (int32_t)cy };
        arc.radius = 11;
        arc.start_angle = 0;
        arc.end_angle = 360;
        arc.width = 5;
        lv_draw_arc(layer, &arc);
        ln.p1 = (lv_point_precise_t){ cx, cy + 24 };
        ln.p2 = (lv_point_precise_t){ cx, cy + 11 };
        lv_draw_line(layer, &ln);
        ln.p1 = (lv_point_precise_t){ cx + 8, cy - 8 };
        ln.p2 = (lv_point_precise_t){ cx + 15, cy - 15 };
        lv_draw_line(layer, &ln);
        tri(layer, c, LV_OPA_COVER, cx + 23, cy - 23, cx + 9, cy - 19, cx + 19, cy - 9);
        return;
    }
    static const float k_deg[NAV_M_COUNT] = { [NAV_M_SLIGHT_LEFT] = -45, [NAV_M_LEFT] = -90, [NAV_M_SHARP_LEFT] = -135,
                                              [NAV_M_SLIGHT_RIGHT] = 45, [NAV_M_RIGHT] = 90, [NAV_M_SHARP_RIGHT] = 135 };
    float th = k_deg[s_nav_kind] * (float)M_PI / 180.0f, dx = sinf(th), dy = -cosf(th);
    bool straight = th == 0.0f;
    float my = straight ? cy - 8 : cy + 2;
    ln.p1 = (lv_point_precise_t){ cx, cy + 24 };
    ln.p2 = (lv_point_precise_t){ cx, my };
    lv_draw_line(layer, &ln);
    float ex = cx, ey = my;
    if(!straight) {
        ex = cx + 16 * dx, ey = my + 16 * dy;
        ln.p1 = (lv_point_precise_t){ cx, my };
        ln.p2 = (lv_point_precise_t){ ex, ey };
        lv_draw_line(layer, &ln);
    }
    float px = -dy, py = dx;   /* across the arrow */
    tri(layer, c, LV_OPA_COVER, ex + 12 * dx, ey + 12 * dy, ex + 9 * px, ey + 9 * py, ex - 9 * px, ey - 9 * py);
}

void cockpit_set_nav(const nav_banner_t *b, const char *via, int progress_permille)
{
    if(!s_ck.nav_card) return;
    if(!b) {
        lv_obj_set_hidden(s_ck.nav_card, true);
        return;
    }
    lv_obj_set_hidden(lv_obj_get_parent(s_nav_icon), !b->turn);
    if(b->turn) {
        if(s_nav_kind != b->kind) {
            s_nav_kind = b->kind;
            lv_obj_invalidate(s_nav_icon);
        }
        lv_label_set_text(s_nav_turn, b->turn_dist);
        lv_label_set_text(s_nav_instr, b->instruction);
    }
    char t[112];
    snprintf(t, sizeof t, "%s", b->dest[0] ? b->dest : "Destination");
    if(t[0] >= 'a' && t[0] <= 'z') t[0] = (char)(t[0] - 32);
    lv_label_set_text(s_nav_dest, t);
    lv_obj_set_hidden(s_nav_via, !(via && *via));
    if(via && *via) lv_label_set_text_fmt(s_nav_via, "via %s", via);
    lv_label_set_text(s_nav_trip, b->trip);
    lv_bar_set_value(s_nav_bar, progress_permille < 0 ? 0 : progress_permille > 1000 ? 1000 : progress_permille,
                     LV_ANIM_OFF);
    lv_obj_set_hidden(s_ck.nav_card, false);
}

static lv_obj_t *overlay_card(lv_obj_t *parent, int32_t w)
{
    lv_obj_t *c = glass(parent, 22);
    lv_obj_set_size(c, w, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(c, 24, 0);
    lv_obj_set_style_pad_ver(c, 18, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(c, 8, 0);
    lv_obj_set_hidden(c, true);
    return c;
}

static void map_page(lv_obj_t *tile, int32_t w, int32_t h)
{
    s_ck.map = map_view_create(tile, &(map_view_cfg_t){ .lat = 37.4419, .lon = -122.1430, .zoom = 16 });
    lv_obj_set_size(s_ck.map, w, h);

    lv_obj_t *ctl = ui_col(tile, 10);
    lv_obj_set_width(ctl, LV_SIZE_CONTENT);
    lv_obj_align(ctl, LV_ALIGN_RIGHT_MID, -24, 0);
    round_btn(ctl, "+", 56, zoom_click, (void *)1);
    round_btn(ctl, "\xE2\x88\x92", 56, zoom_click, NULL);
    round_btn(ctl, NULL, 56, recenter_click, NULL);
    icon_btn(ctl, ICON_OVERVIEW, overview_click);
    s_compass_btn = icon_btn(ctl, ICON_COMPASS, compass_click);
    lv_timer_create(compass_poll, 100, NULL);
    s_theme_btn = glass(ctl, 16);
    lv_obj_set_size(s_theme_btn, 56, 56);
    lv_obj_set_clickable(s_theme_btn, true);
    lv_obj_set_style_bg_opa(s_theme_btn, 60, LV_STATE_PRESSED);
    lv_obj_add_event_cb(s_theme_btn, icon_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)ICON_THEME);
    lv_obj_add_event_cb(s_theme_btn, theme_click, LV_EVENT_CLICKED, NULL);

    s_ck.src_chip = ui_chip(tile, "Position \xC2\xB7 waiting", CB_TEXT_MID, false);
    lv_obj_set_style_bg_color(s_ck.src_chip, lv_color_hex(0x0e1117), 0);
    lv_obj_set_style_bg_opa(s_ck.src_chip, 215, 0);
    lv_obj_align(s_ck.src_chip, LV_ALIGN_TOP_RIGHT, -24, 76);

    s_ck.nav_card = overlay_card(tile, 480);
    lv_obj_align(s_ck.nav_card, LV_ALIGN_TOP_LEFT, 24, 76);
    lv_obj_t *turn = ui_row(s_ck.nav_card, 16);
    s_nav_icon = ui_plain(turn);
    lv_obj_set_size(s_nav_icon, 64, 64);
    lv_obj_add_event_cb(s_nav_icon, maneuver_draw, LV_EVENT_DRAW_MAIN_END, NULL);
    lv_obj_t *tc = ui_col(turn, 2);
    lv_obj_set_width(tc, 0);
    lv_obj_set_flex_grow(tc, 1);
    s_nav_turn = ui_label(tc, "", CB_F_TEMP, CB_CYAN);
    s_nav_instr = ui_label(tc, "", CB_F_VALUE, CB_TEXT_HI);
    lv_obj_set_width(s_nav_instr, lv_pct(100));
    lv_label_set_long_mode(s_nav_instr, LV_LABEL_LONG_MODE_WRAP);
    s_nav_dest = ui_label_line(s_ck.nav_card, "", CB_F_BODY, CB_TEXT_HI);
    s_nav_via = ui_label_line(s_ck.nav_card, "", CB_F_LABEL, CB_TEXT_MID);
    s_nav_trip = ui_label(s_ck.nav_card, "", CB_F_BODY, CB_TEXT_2);
    s_nav_bar = lv_bar_create(s_ck.nav_card);
    lv_obj_set_size(s_nav_bar, lv_pct(100), 4);
    lv_bar_set_range(s_nav_bar, 0, 1000);
    lv_obj_set_style_bg_color(s_nav_bar, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(s_nav_bar, 30, 0);
    lv_obj_set_style_bg_color(s_nav_bar, cb_col(CB_CYAN), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_nav_bar, LV_OPA_COVER, LV_PART_INDICATOR);

    s_ck.toast = overlay_card(tile, 520);
    lv_obj_align(s_ck.toast, LV_ALIGN_BOTTOM_LEFT, 24, -44);   /* clear of the Mapbox wordmark */
}

static lv_obj_t *page_col(lv_obj_t *tile, int32_t pad)
{
    lv_obj_t *c = ui_col(tile, 22);
    lv_obj_set_size(c, lv_pct(100), lv_pct(100));
    lv_obj_set_style_pad_hor(c, pad + 6, 0);
    lv_obj_set_style_pad_top(c, 76, 0);
    lv_obj_set_style_pad_bottom(c, pad, 0);
    lv_obj_set_style_bg_color(c, lv_color_hex(0x0e1117), 0);
    lv_obj_set_style_bg_opa(c, 200, 0);
    lv_obj_set_scrollable(c, true);
    lv_obj_set_scroll_dir(c, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(c, LV_SCROLLBAR_MODE_OFF);
    return c;
}

static lv_obj_t *section(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_add_style(l, &cb_st_label, 0);
    lv_label_set_text(l, text);
    return l;
}

static void right_side(lv_obj_t *panel, int32_t x, int32_t y, int32_t w, int32_t h, lv_obj_t **media_parent,
                       lv_obj_t **nova_cols)
{
    lv_obj_t *frame = glass(panel, 26);
    lv_obj_set_size(frame, w, h);
    lv_obj_set_pos(frame, x, y);
    lv_obj_set_style_bg_opa(frame, 0, 0);

    s_tv = lv_tileview_create(frame);
    lv_obj_remove_style_all(s_tv);
    lv_obj_set_size(s_tv, w - 2, h - 2);
    lv_obj_center(s_tv);
    lv_obj_set_scrollbar_mode(s_tv, LV_SCROLLBAR_MODE_OFF);
    lv_obj_add_event_cb(s_tv, tile_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_t *t0 = lv_tileview_add_tile(s_tv, 0, 0, LV_DIR_RIGHT);
    lv_obj_t *t1 = lv_tileview_add_tile(s_tv, 1, 0, LV_DIR_HOR);
    lv_obj_t *t2 = lv_tileview_add_tile(s_tv, 2, 0, LV_DIR_LEFT);

    map_page(t0, w - 2, h - 2);

    lv_obj_t *media = page_col(t1, 34);
    section(media, "NOW PLAYING \xC2\xB7 SIMULATED");
    *media_parent = media;

    lv_obj_t *nova = page_col(t2, 34);
    lv_obj_set_flex_flow(nova, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(nova, 26, 0);
    lv_obj_t *c0 = ui_col(nova, 16), *c1 = ui_col(nova, 16);
    lv_obj_set_width(c0, 0);
    lv_obj_set_width(c1, 0);
    lv_obj_set_flex_grow(c0, 3);
    lv_obj_set_flex_grow(c1, 2);
    section(c0, "NOVA \xC2\xB7 ANSWERS");
    s_ck.cards = ui_col(c0, 12);
    nova_cols[0] = c0;
    nova_cols[1] = c1;

    lv_obj_t *pager = glass(frame, LV_RADIUS_CIRCLE);
    lv_obj_set_size(pager, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_all(pager, 4, 0);
    lv_obj_set_flex_flow(pager, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(pager, 4, 0);
    lv_obj_align(pager, LV_ALIGN_TOP_MID, 0, 16);
    static const char *names[3] = { "Map", "Media", "Nova" };
    for(int i = 0; i < 3; i++) {
        lv_obj_t *b = ui_plain(pager);
        lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_hor(b, 18, 0);
        lv_obj_set_style_pad_ver(b, 8, 0);
        lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(b, lv_color_white(), 0);
        lv_obj_set_clickable(b, true);
        ui_label(b, names[i], CB_F_BODY, CB_TEXT_LO);
        lv_obj_add_event_cb(b, pager_click, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        s_pager_btn[i] = b;
    }
}

/* ---------- dock ---------- */

static void temp_obs(lv_observer_t *o, lv_subject_t *s)
{
    int32_t v = lv_subject_get_int(s);
    lv_label_set_text_fmt(lv_observer_get_target_obj(o), "%d.%d\xC2\xB0", (int)(v / 10), (int)(v % 10));
}

static void temp_click(lv_event_t *e)
{
    intptr_t code = (intptr_t)lv_event_get_user_data(e);   /* bit 1 = driver, bit 0 = warmer */
    if(s_hooks.temp_step) s_hooks.temp_step((code & 2) != 0, (code & 1) ? 5 : -5);
}

static lv_obj_t *temp_button(lv_obj_t *row, bool driver, bool warmer)
{
    lv_obj_t *b = ui_plain(row);
    lv_obj_set_size(b, 56, 56);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(b, 18, 0);
    lv_obj_set_style_bg_opa(b, 60, LV_STATE_PRESSED);
    lv_obj_set_clickable(b, true);
    lv_obj_set_ext_click_area(b, 10);
    lv_obj_center(ui_label(b, warmer ? "+" : "\xE2\x88\x92", CB_F_TEMP, CB_TEXT_2));
    lv_obj_add_event_cb(b, temp_click, LV_EVENT_CLICKED, (void *)(intptr_t)((driver ? 2 : 0) | (warmer ? 1 : 0)));
    return b;
}

static void temp_control(lv_obj_t *parent, bool driver)
{
    lv_obj_t *row = ui_row(parent, 12);
    lv_obj_set_width(row, LV_SIZE_CONTENT);
    temp_button(row, driver, false);
    lv_obj_t *col = ui_col(row, 0);
    lv_obj_set_width(col, 104);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *v = ui_label(col, "--", CB_F_TEMP, CB_TEXT_HI);
    lv_subject_add_observer_obj(driver ? cb_model()->driver_temp_dc : cb_model()->passenger_temp_dc, temp_obs, v, NULL);
    lv_obj_t *k = ui_label(col, driver ? "DRIVER" : "PASSENGER", CB_F_SMALL, CB_TEXT_LO);
    lv_obj_set_style_text_letter_space(k, 2, 0);
    temp_button(row, driver, true);
}

static void mini_obs(lv_observer_t *o, lv_subject_t *s)
{
    lv_label_set_text(lv_observer_get_target_obj(o), cb_model_track_title(lv_subject_get_int(s)));
}

static void play_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(s);
    lv_obj_invalidate(lv_observer_get_target_obj(o));
}

static void play_click(lv_event_t *e)
{
    LV_UNUSED(e);
    lv_subject_t *p = cb_model()->media_playing;
    cb_model_set_int(p, !lv_subject_get_int(p));
}

static void dock(lv_obj_t *panel, int32_t w, int32_t h, int32_t dock_h)
{
    int32_t y = h - dock_h;
    lv_obj_t *left = ui_row(panel, 0);
    lv_obj_set_size(left, LV_SIZE_CONTENT, dock_h);
    lv_obj_set_pos(left, SIDE, y);
    temp_control(left, true);

    s_ck.dock_mid = ui_plain(panel);
    lv_obj_set_size(s_ck.dock_mid, BAR_SLOT_W, dock_h);
    lv_obj_set_pos(s_ck.dock_mid, (w - BAR_SLOT_W) / 2, y);
    lv_obj_set_overflow_visible(s_ck.dock_mid, true);

    lv_obj_t *right = ui_row(panel, 20);
    lv_obj_set_size(right, LV_SIZE_CONTENT, dock_h);
    lv_obj_align(right, LV_ALIGN_TOP_RIGHT, -SIDE, y);
    lv_obj_t *mini = ui_row(right, 12);
    lv_obj_set_width(mini, LV_SIZE_CONTENT);
    lv_obj_t *art = ui_plain(mini);
    lv_obj_set_size(art, 52, 52);
    lv_obj_set_style_radius(art, 12, 0);
    lv_obj_set_style_bg_opa(art, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(art, cb_col(CB_VIOLET), 0);
    lv_obj_set_style_bg_grad_color(art, cb_col(CB_CYAN), 0);
    lv_obj_set_style_bg_grad_dir(art, LV_GRAD_DIR_HOR, 0);
    lv_obj_t *t = ui_col(mini, 2);
    lv_obj_set_width(t, 140);
    lv_obj_t *title = ui_label_line(t, "", CB_F_VALUE, CB_TEXT_HI);
    lv_subject_add_observer_obj(cb_model()->media_track, mini_obs, title, NULL);
    ui_label(t, "Simulated media", CB_F_LABEL, CB_TEXT_MID);
    lv_obj_t *pb = ui_plain(mini);
    lv_obj_set_size(pb, 50, 50);
    lv_obj_set_style_radius(pb, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(pb, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(pb, 20, 0);
    lv_obj_set_clickable(pb, true);
    lv_obj_add_event_cb(pb, icon_draw, LV_EVENT_DRAW_MAIN_END, (void *)(intptr_t)ICON_MEDIA);
    lv_subject_add_observer_obj(cb_model()->media_playing, play_obs, pb, NULL);
    lv_obj_add_event_cb(pb, play_click, LV_EVENT_CLICKED, NULL);
    temp_control(right, false);
}

/* ---------- screen ---------- */

cockpit_t *cockpit_create(lv_obj_t *panel, const cockpit_hooks_t *hooks)
{
    s_hooks = *hooks;
    lv_obj_update_layout(panel);
    int32_t w = lv_obj_get_width(panel), h = lv_obj_get_height(panel);
    bool tall = h >= 900;
    int32_t strip_h = tall ? 72 : 60, top = tall ? 84 : 68, dock_h = tall ? 116 : 104;
    int32_t body_h = h - top - dock_h - (tall ? 20 : 8);

    status_strip(panel, w, strip_h);

    lv_obj_t *left = glass(panel, 26);
    lv_obj_set_size(left, LEFT_W, body_h);
    lv_obj_set_pos(left, SIDE, top);
    lv_obj_set_style_pad_all(left, tall ? 24 : 16, 0);
    lv_obj_set_flex_flow(left, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(left, tall ? 16 : 10, 0);
    lv_obj_set_scrollable(left, true);
    lv_obj_set_scroll_dir(left, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(left, LV_SCROLLBAR_MODE_OFF);
    section(left, "CAR");
    drive_build_speed(left);
    lv_obj_t *climate = ui_col(left, 0), *tiles = ui_col(left, 0);
    lv_obj_set_height(tiles, tall ? 420 : 340);   /* 3 x 4 tiles */

    lv_obj_t *media_parent, *nova_cols[2];
    int32_t rx = SIDE + LEFT_W + GAP;
    right_side(panel, rx, top, w - rx - SIDE, body_h, &media_parent, nova_cols);
    comfort_build_parts(climate, tiles, media_parent);
    connected_build_parts(nova_cols[0], nova_cols[1], nova_cols[1]);

    dock(panel, w, h, dock_h);
    pager_sync(0);
    return &s_ck;
}

cockpit_t *cockpit_create_v2(lv_obj_t *panel, const cockpit_hooks_t *hooks)
{
    s_hooks = *hooks;
    lv_obj_update_layout(panel);
    int32_t w = lv_obj_get_width(panel), h = lv_obj_get_height(panel);
    bool tall = h >= 900;
    int32_t strip_h = tall ? 72 : 60, top = tall ? 84 : 68, dock_h = tall ? 116 : 104;
    int32_t body_h = h - top - dock_h - (tall ? 20 : 8);

    status_strip(panel, w, strip_h);

    /* Nova on top with the tiles right under it (the sketch's stack): the open panel pushes them down within the
       column, never off it, so a tile's glow stays in view while Nova talks. */
    int32_t gap = tall ? 16 : 10;
    lv_obj_t *left = ui_col(panel, gap);
    lv_obj_set_size(left, LEFT_W, body_h);
    lv_obj_set_pos(left, SIDE, top);
    lv_obj_set_overflow_visible(left, true);
    s_ck.nova_slot = ui_col(left, 0);
    lv_obj_set_overflow_visible(s_ck.nova_slot, true);
    lv_obj_t *tools = glass(left, 26);
    lv_obj_set_size(tools, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_all(tools, tall ? 24 : 16, 0);
    lv_obj_set_flex_flow(tools, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(tools, tall ? 14 : 8, 0);
    section(tools, "IN-CAR TOOLS");
    lv_obj_t *tiles = ui_col(tools, 0);
    lv_obj_set_height(tiles, tall ? 420 : 340);   /* 3 x 4 tiles */
    lv_obj_t *climate = ui_col(panel, 0);         /* the climate card: the dock's -/+ already say it all */
    lv_obj_set_hidden(climate, true);

    lv_obj_t *media_parent, *nova_cols[2];
    int32_t rx = SIDE + LEFT_W + GAP;
    right_side(panel, rx, top, w - rx - SIDE, body_h, &media_parent, nova_cols);
    comfort_set_flash_style(COMFORT_FLASH_GRADIENT);
    comfort_build_parts(climate, tiles, media_parent);
    connected_build_parts(nova_cols[0], nova_cols[1], nova_cols[1]);

    lv_obj_update_layout(left);
    s_ck.nova_max_h = body_h - lv_obj_get_height(tools) - gap;
    dock(panel, w, h, dock_h);
    drive_build_speed(s_ck.dock_mid);   /* Nova moved up: the speed takes the dock's middle */
    pager_sync(0);
    return &s_ck;
}
