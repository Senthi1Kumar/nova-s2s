#include "cb_theme.h"
#include "cb_fonts.h"

static const uint32_t k_colors[CB_COLOR_COUNT] = {
    0x07080b, 0x0e1016, 0x151922, 0x1e2330,
    0x4cd6ff, 0xb66bff, 0xff5e7e, 0xff9a4d, 0x4dffb4, 0xffd84d,
    0xeef1f7, 0xc9ced9, 0x8b93a7, 0x4d5566, 0x5d6678,
};

static const lv_font_t *const k_fonts[CB_FONT_COUNT] = {
    &cb_inter_11, &cb_inter_12, &cb_inter_14, &cb_inter_16, &cb_inter_20, &cb_inter_34, &cb_inter_52,
    &cb_mono_11, &cb_mono_12, &cb_mono_14,
};

lv_style_t cb_st_panel;
lv_style_t cb_st_card;
lv_style_t cb_st_chip;
lv_style_t cb_st_label;

static bool s_inited;
static bool s_reduce;

void cb_theme_set_reduce_motion(bool on) { s_reduce = on; }

bool cb_reduce_motion(void) { return s_reduce; }

lv_color_t cb_col(cb_color_t c)
{
    return lv_color_hex(k_colors[c < CB_COLOR_COUNT ? c : CB_TEXT_HI]);
}

const lv_font_t *cb_font(cb_font_t f)
{
    return k_fonts[f < CB_FONT_COUNT ? f : CB_F_BODY];
}

void cb_theme_init(void)
{
    if(s_inited) return;
    s_inited = true;

    lv_style_init(&cb_st_panel);
    lv_style_set_bg_color(&cb_st_panel, lv_color_white());
    lv_style_set_bg_opa(&cb_st_panel, 11);
    lv_style_set_border_color(&cb_st_panel, lv_color_white());
    lv_style_set_border_opa(&cb_st_panel, 23);
    lv_style_set_border_width(&cb_st_panel, 1);
    lv_style_set_radius(&cb_st_panel, 20);
    lv_style_set_pad_hor(&cb_st_panel, 18);
    lv_style_set_pad_ver(&cb_st_panel, 16);
    lv_style_set_pad_row(&cb_st_panel, 12);
    lv_style_set_text_color(&cb_st_panel, cb_col(CB_TEXT_HI));
    lv_style_set_text_font(&cb_st_panel, cb_font(CB_F_BODY));

    lv_style_init(&cb_st_card);
    lv_style_set_bg_color(&cb_st_card, lv_color_white());
    lv_style_set_bg_opa(&cb_st_card, 9);
    lv_style_set_border_width(&cb_st_card, 0);
    lv_style_set_radius(&cb_st_card, 14);
    lv_style_set_pad_hor(&cb_st_card, 14);
    lv_style_set_pad_ver(&cb_st_card, 12);
    lv_style_set_pad_row(&cb_st_card, 8);

    lv_style_init(&cb_st_chip);
    lv_style_set_bg_color(&cb_st_chip, lv_color_white());
    lv_style_set_bg_opa(&cb_st_chip, 15);
    lv_style_set_border_width(&cb_st_chip, 0);
    lv_style_set_radius(&cb_st_chip, LV_RADIUS_CIRCLE);
    lv_style_set_pad_hor(&cb_st_chip, 10);
    lv_style_set_pad_ver(&cb_st_chip, 3);
    lv_style_set_pad_column(&cb_st_chip, 6);
    lv_style_set_text_font(&cb_st_chip, cb_font(CB_F_LABEL));
    lv_style_set_text_color(&cb_st_chip, cb_col(CB_TEXT_2));

    lv_style_init(&cb_st_label);
    lv_style_set_text_font(&cb_st_label, cb_font(CB_F_LABEL));
    lv_style_set_text_letter_space(&cb_st_label, 2);
    lv_style_set_text_color(&cb_st_label, cb_col(CB_TEXT_MID));
}

lv_obj_t *cb_panel_create(lv_obj_t *screen)
{
    lv_obj_set_style_bg_color(screen, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(screen, false);

    lv_obj_t *p = lv_obj_create(screen);
    lv_obj_remove_style_all(p);
    lv_obj_set_size(p, CB_PANEL_W, CB_PANEL_H);
    lv_obj_set_style_bg_color(p, cb_col(CB_BASE), 0);
    lv_obj_set_style_bg_opa(p, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(p, false);
    lv_obj_center(p);   /* alignment survives window and fullscreen resizes */
    return p;
}
