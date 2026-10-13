#include "screens/gate.h"
#include "cb_theme.h"
#include "widgets/sparkline.h"

#define CELL_W 456
#define CELL_H 330
#define GAP 16

static lv_obj_t *s_spark;

static lv_obj_t *cell(lv_obj_t *panel, int idx, const char *title)
{
    int col = idx % 4, row = idx / 4;
    lv_obj_t *c = lv_obj_create(panel);
    lv_obj_remove_style_all(c);
    lv_obj_add_style(c, &cb_st_card, 0);
    lv_obj_set_size(c, CELL_W, CELL_H);
    lv_obj_set_pos(c, 24 + col * (CELL_W + GAP), 22 + row * (CELL_H + GAP));
    lv_obj_set_scrollable(c, false);
    lv_obj_t *t = lv_label_create(c);
    lv_obj_add_style(t, &cb_st_label, 0);
    lv_label_set_text(t, title);
    lv_obj_align(t, LV_ALIGN_TOP_LEFT, 0, 0);
    return c;
}

static lv_obj_t *box(lv_obj_t *parent, int32_t w, int32_t h, int32_t radius)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(o, false);
    return o;
}

static void anim_x(void *o, int32_t v) { lv_obj_set_x(o, v); }
static void anim_w(void *o, int32_t v) { lv_obj_set_width(o, v); }
static void anim_opa(void *o, int32_t v) { lv_obj_set_style_opa(o, (lv_opa_t)v, 0); }
static void anim_arc(void *o, int32_t v) { lv_arc_set_value(o, (int16_t)v); }

static void loop_anim(void *obj, lv_anim_exec_xcb_t cb, int32_t from, int32_t to, uint32_t ms, lv_anim_path_cb_t path)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_exec_cb(&a, cb);
    lv_anim_set_values(&a, from, to);
    lv_anim_set_duration(&a, ms);
    lv_anim_set_reverse_duration(&a, ms);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&a, path ? path : lv_anim_path_ease_in_out);
    lv_anim_start(&a);
}

static void spark_timer_cb(lv_timer_t *t)
{
    static int32_t v = 60;
    LV_UNUSED(t);
    v += (int32_t)lv_rand(0, 8) - 4;
    v = LV_CLAMP(45, v, 80);
    sparkline_push(s_spark, v);
}

void gate_screen_create(lv_obj_t *panel)
{
    /* 1: glass panel with backdrop blur over a moving object */
    lv_obj_t *c = cell(panel, 0, "1 GLASS + BACKDROP BLUR");
    lv_obj_t *dot = box(c, 90, 90, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_color(dot, cb_col(CB_CYAN), 0);
    lv_obj_set_y(dot, 120);
    loop_anim(dot, anim_x, 0, 330, 2000, NULL);
    lv_obj_t *glass = lv_obj_create(c);
    lv_obj_remove_style_all(glass);
    lv_obj_add_style(glass, &cb_st_panel, 0);
    lv_obj_set_size(glass, 320, 150);
    lv_obj_align(glass, LV_ALIGN_CENTER, 0, 20);
    lv_obj_set_style_blur_backdrop(glass, true, 0);
    lv_obj_set_style_blur_radius(glass, 16, 0);

    /* 2: the orb body: 2-stop vertical gradient */
    c = cell(panel, 1, "2 ORB · 2-STOP VERTICAL");
    lv_obj_t *orb = box(c, 170, 170, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_color(orb, lv_color_hex(0xf7fafc), 0);
    lv_obj_set_style_bg_grad_color(orb, cb_col(CB_GREEN), 0);
    lv_obj_set_style_bg_grad_dir(orb, LV_GRAD_DIR_VER, 0);
    lv_obj_align(orb, LV_ALIGN_CENTER, 0, 14);

    /* 3: 3-stop horizontal gradient (NanoVG renders 2 stops; this shows how it degrades) */
    c = cell(panel, 2, "3 THREE-STOP HORIZONTAL");
    static lv_grad_dsc_t g3;
    const lv_color_t cols3[3] = { cb_col(CB_VIOLET), cb_col(CB_CYAN), cb_col(CB_GREEN) };
    const lv_opa_t opas3[3] = { 255, 255, 255 };
    const uint8_t fracs3[3] = { 0, 128, 255 };
    lv_grad_init_stops(&g3, cols3, opas3, fracs3, 3);
    lv_grad_horizontal_init(&g3);
    lv_obj_t *bar3 = box(c, 400, 80, 14);
    lv_obj_set_style_bg_grad(bar3, &g3, 0);
    lv_obj_align(bar3, LV_ALIGN_CENTER, 0, 14);

    /* 4: radial glow */
    c = cell(panel, 3, "4 RADIAL GLOW");
    static lv_grad_dsc_t gr;
    const lv_color_t colsr[2] = { cb_col(CB_CYAN), cb_col(CB_CYAN) };
    const lv_opa_t opasr[2] = { 150, 0 };
    const uint8_t fracsr[2] = { 0, 255 };
    lv_grad_init_stops(&gr, colsr, opasr, fracsr, 2);
    lv_grad_radial_init(&gr, LV_GRAD_CENTER, LV_GRAD_CENTER, LV_GRAD_RIGHT, LV_GRAD_CENTER, LV_GRAD_EXTEND_PAD);
    lv_obj_t *glow = box(c, 240, 240, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_grad(glow, &gr, 0);
    lv_obj_align(glow, LV_ALIGN_CENTER, 0, 14);
    lv_obj_t *core = box(glow, 60, 60, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_color(core, cb_col(CB_CYAN), 0);
    lv_obj_center(core);

    /* 5: arc gauge */
    c = cell(panel, 4, "5 ARC GAUGE");
    lv_obj_t *arc = lv_arc_create(c);
    lv_obj_set_size(arc, 220, 220);
    lv_arc_set_bg_angles(arc, 135, 45);
    lv_arc_set_range(arc, 0, 100);
    lv_obj_set_clickable(arc, false);
    lv_obj_set_style_arc_width(arc, 12, LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 12, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_arc_opa(arc, 28, LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, cb_col(CB_GREEN), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(arc, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_pad_all(arc, 0, LV_PART_KNOB);
    lv_obj_align(arc, LV_ALIGN_CENTER, 0, 20);
    loop_anim(arc, anim_arc, 0, 100, 1000, NULL);

    /* 6: sparkline with area fill */
    c = cell(panel, 5, "6 SPARKLINE + AREA FILL");
    s_spark = sparkline_create(c, 420, 220, 40, CB_CYAN, 40, 85);
    lv_obj_align(s_spark, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_timer_create(spark_timer_cb, 100, NULL);

    /* 7: every font role + a shadow card */
    c = cell(panel, 6, "7 FONTS + SHADOW");
    static const cb_font_t roles[] = { CB_F_SMALL, CB_F_LABEL, CB_F_BODY, CB_F_VALUE, CB_F_TITLE, CB_F_TEMP,
                                       CB_F_MONO_S, CB_F_MONO, CB_F_MONO_L };
    int32_t y = 24;
    for(size_t i = 0; i < sizeof(roles) / sizeof(roles[0]); i++) {
        lv_obj_t *l = lv_label_create(c);
        lv_label_set_text(l, "Nova 62 km/h \xC2\xB7 21\xC2\xB0 \xE2\x86\x92 ok");
        lv_obj_set_style_text_font(l, cb_font(roles[i]), 0);
        lv_obj_set_style_text_color(l, cb_col(CB_TEXT_HI), 0);
        lv_obj_set_pos(l, 0, y);
        y += lv_font_get_line_height(cb_font(roles[i])) + 2;
    }
    lv_obj_t *sh = box(c, 110, 70, 14);
    lv_obj_set_style_bg_color(sh, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_shadow_width(sh, 24, 0);
    lv_obj_set_style_shadow_color(sh, cb_col(CB_VIOLET), 0);
    lv_obj_set_style_shadow_opa(sh, 100, 0);
    lv_obj_align(sh, LV_ALIGN_BOTTOM_RIGHT, -10, -10);
    lv_obj_t *big = lv_label_create(c);
    lv_label_set_text(big, "62");
    lv_obj_set_style_text_font(big, cb_font(CB_F_SPEED), 0);
    lv_obj_set_style_text_color(big, cb_col(CB_TEXT_HI), 0);
    lv_obj_align(big, LV_ALIGN_TOP_RIGHT, -10, 24);

    /* 8: tuck stress: width + fade, the Nova bar's animation shape */
    c = cell(panel, 7, "8 TUCK STRESS");
    lv_obj_t *pill = box(c, 420, 72, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_color(pill, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(pill, 18, 0);
    lv_obj_set_style_border_color(pill, lv_color_white(), 0);
    lv_obj_set_style_border_opa(pill, 23, 0);
    lv_obj_set_style_border_width(pill, 1, 0);
    lv_obj_align(pill, LV_ALIGN_LEFT_MID, 0, 14);
    lv_obj_t *pl = lv_label_create(pill);
    lv_label_set_text(pl, "LISTENING  what's the weather...");
    lv_obj_set_style_text_font(pl, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_text_color(pl, cb_col(CB_TEXT_HI), 0);
    lv_obj_align(pl, LV_ALIGN_LEFT_MID, 70, 0);
    lv_obj_t *porb = box(pill, 52, 52, LV_RADIUS_CIRCLE);
    lv_obj_set_style_bg_color(porb, lv_color_hex(0xf7fafc), 0);
    lv_obj_set_style_bg_grad_color(porb, cb_col(CB_CYAN), 0);
    lv_obj_set_style_bg_grad_dir(porb, LV_GRAD_DIR_VER, 0);
    lv_obj_align(porb, LV_ALIGN_LEFT_MID, 10, 0);
    loop_anim(pill, anim_w, 420, 108, 420, lv_anim_path_ease_out);
    loop_anim(pl, anim_opa, 255, 0, 420, NULL);
}
