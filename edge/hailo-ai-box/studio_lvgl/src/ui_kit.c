#include "ui_kit.h"
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

lv_obj_t *ui_panel(lv_obj_t *parent, const char *title)
{
    lv_obj_t *p = ui_plain(parent);
    lv_obj_add_style(p, &cb_st_panel, 0);
    lv_obj_set_flex_flow(p, LV_FLEX_FLOW_COLUMN);
    lv_obj_t *head = ui_row(p, 8);
    lv_obj_t *t = lv_label_create(head);
    lv_obj_add_style(t, &cb_st_label, 0);
    lv_label_set_text(t, title);
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

static lv_obj_t *icon_part(lv_obj_t *icon, int32_t x, int32_t y, int32_t w, int32_t h, int32_t r, bool fill, cb_color_t c)
{
    lv_obj_t *o = ui_plain(icon);
    lv_obj_set_size(o, w, h);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_style_radius(o, r, 0);
    if(fill) {
        lv_obj_set_style_bg_color(o, cb_col(c), 0);
        lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    }
    else {
        lv_obj_set_style_border_color(o, cb_col(c), 0);
        lv_obj_set_style_border_width(o, 2, 0);
    }
    return o;
}

lv_obj_t *ui_icon(lv_obj_t *parent, ui_icon_t kind, cb_color_t c)
{
    lv_obj_t *ic = ui_plain(parent);
    lv_obj_set_size(ic, 22, 22);
    lv_obj_set_style_text_color(ic, cb_col(c), 0);
    const char *sym = NULL;
    switch(kind) {
        case UI_ICON_GRID:
            icon_part(ic, 1, 1, 8, 8, 2, true, c);
            icon_part(ic, 13, 1, 8, 8, 2, true, c);
            icon_part(ic, 1, 13, 8, 8, 2, true, c);
            icon_part(ic, 13, 13, 8, 8, 2, true, c);
            break;
        case UI_ICON_MIC: {
            icon_part(ic, 7, 0, 8, 13, 4, true, c);
            lv_obj_t *cup = icon_part(ic, 3, 6, 16, 11, 8, false, c);
            lv_obj_set_style_border_side(cup, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_LEFT | LV_BORDER_SIDE_RIGHT, 0);
            icon_part(ic, 10, 17, 2, 4, 0, true, c);
            icon_part(ic, 6, 20, 10, 2, 1, true, c);
            break;
        }
        case UI_ICON_CHAT:
            icon_part(ic, 1, 2, 20, 14, 5, false, c);
            icon_part(ic, 5, 16, 4, 5, 1, true, c);
            break;
        case UI_ICON_FACE:
            icon_part(ic, 6, 1, 10, 10, LV_RADIUS_CIRCLE, true, c);
            icon_part(ic, 2, 13, 18, 8, 8, true, c);
            break;
        case UI_ICON_EYE: sym = LV_SYMBOL_EYE_OPEN; break;
        case UI_ICON_GEAR: sym = LV_SYMBOL_SETTINGS; break;
        case UI_ICON_LIST: sym = LV_SYMBOL_LIST; break;
        case UI_ICON_CHIP: sym = LV_SYMBOL_CHARGE; break;
        case UI_ICON_SPEAKER: sym = LV_SYMBOL_VOLUME_MAX; break;
        case UI_ICON_IMAGE: sym = LV_SYMBOL_IMAGE; break;
    }
    if(sym) {
        lv_obj_t *l = lv_label_create(ic);
        lv_label_set_text(l, sym);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(l, cb_col(c), 0);
        lv_obj_center(l);
    }
    return ic;
}

void ui_icon_set_color(lv_obj_t *icon, cb_color_t c)
{
    lv_color_t col = cb_col(c);
    uint32_t n = lv_obj_get_child_count(icon);
    for(uint32_t i = 0; i < n; i++) {
        lv_obj_t *o = lv_obj_get_child(icon, (int32_t)i);
        lv_obj_set_style_bg_color(o, col, 0);
        lv_obj_set_style_border_color(o, col, 0);
        lv_obj_set_style_text_color(o, col, 0);
    }
}
