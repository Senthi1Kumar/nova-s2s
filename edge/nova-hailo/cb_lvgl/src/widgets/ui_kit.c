#include "widgets/ui_kit.h"
#include "cb_ambient.h"

lv_obj_t *ui_plain(lv_obj_t *parent)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_scrollable(o, false);
    lv_obj_set_clickable(o, false);
    return o;
}

lv_obj_t *ui_row(lv_obj_t *parent, int32_t gap)
{
    lv_obj_t *o = ui_plain(parent);
    lv_obj_set_size(o, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(o, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(o, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(o, gap, 0);
    return o;
}

lv_obj_t *ui_col(lv_obj_t *parent, int32_t gap)
{
    lv_obj_t *o = ui_plain(parent);
    lv_obj_set_size(o, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(o, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(o, gap, 0);
    return o;
}

lv_obj_t *ui_spacer(lv_obj_t *row)
{
    lv_obj_t *s = ui_plain(row);
    lv_obj_set_size(s, 1, 1);
    lv_obj_set_flex_grow(s, 1);
    return s;
}

lv_obj_t *ui_label(lv_obj_t *parent, const char *text, cb_font_t f, cb_color_t c)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, cb_font(f), 0);
    lv_obj_set_style_text_color(l, cb_col(c), 0);
    return l;
}

/* DOTS only truncates when the height is fixed; a content-sized label would wrap instead. */
lv_obj_t *ui_label_line(lv_obj_t *parent, const char *text, cb_font_t f, cb_color_t c)
{
    lv_obj_t *l = ui_label(parent, text, f, c);
    lv_obj_set_size(l, lv_pct(100), lv_font_get_line_height(cb_font(f)));
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_DOTS);
    return l;
}

/* 2.4 s halo on the shared ambient clock; only 5 distinct steps, so most ticks change nothing. */
static void breathe_step(void *ctx, uint32_t now)
{
    lv_obj_t *d = ctx;
    int32_t v = (cb_ambient_wave(now, 2400, 0) * 4 + 500) / 1000;
    if(lv_obj_get_style_outline_width(d, 0) == v) return;
    lv_obj_set_style_outline_width(d, v, 0);
    lv_obj_set_style_outline_opa(d, (lv_opa_t)(110 - v * 22), 0);
}

lv_obj_t *ui_dot(lv_obj_t *parent, cb_color_t c, bool breathing)
{
    lv_obj_t *d = ui_plain(parent);
    lv_obj_set_size(d, 6, 6);
    lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(d, cb_col(c), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    if(breathing && !cb_reduce_motion()) {
        lv_obj_set_style_outline_color(d, cb_col(c), 0);
        lv_obj_set_style_outline_pad(d, 0, 0);
        cb_ambient_add(breathe_step, d);
    }
    return d;
}

lv_obj_t *ui_chip(lv_obj_t *parent, const char *text, cb_color_t dot, bool breathing)
{
    lv_obj_t *c = ui_plain(parent);
    lv_obj_add_style(c, &cb_st_chip, 0);
    lv_obj_set_size(c, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(c, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    ui_dot(c, dot, breathing);
    lv_obj_t *l = lv_label_create(c);
    lv_label_set_text(l, text);
    return c;
}

lv_obj_t *ui_chip_label(lv_obj_t *chip) { return lv_obj_get_child(chip, -1); }

lv_obj_t *ui_badge_sim(lv_obj_t *parent)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(b, cb_col(CB_YELLOW), 0);
    lv_obj_set_style_bg_opa(b, 30, 0);
    lv_obj_set_style_radius(b, 4, 0);
    lv_obj_set_style_pad_hor(b, 6, 0);
    lv_obj_set_style_pad_ver(b, 3, 0);
    lv_obj_t *l = ui_label(b, "SIM", CB_F_SMALL, CB_YELLOW);
    lv_obj_set_style_text_letter_space(l, 1, 0);
    return b;
}

lv_obj_t *ui_panel(lv_obj_t *parent, const char *title)
{
    lv_obj_t *p = ui_plain(parent);
    lv_obj_add_style(p, &cb_st_panel, 0);
    lv_obj_set_flex_flow(p, LV_FLEX_FLOW_COLUMN);
    lv_obj_t *head = ui_row(p, 8);
    lv_obj_t *t = lv_label_create(head);
    lv_obj_add_style(t, &cb_st_label, 0);
    lv_label_set_text(t, title);
    ui_spacer(head);
    ui_badge_sim(head);
    return p;
}

lv_obj_t *ui_card(lv_obj_t *parent)
{
    lv_obj_t *c = ui_plain(parent);
    lv_obj_add_style(c, &cb_st_card, 0);
    lv_obj_set_size(c, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    return c;
}
