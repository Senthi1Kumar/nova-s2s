#include "theme.h"
#include "cb_fonts.h"

/* Graphite palette shared with the web Studio (see the plan, Track C). Role names keep cb_lvgl's enum. */
static const uint32_t k_colors[CB_COLOR_COUNT] = {
    0x0f1216, 0x161b22, 0x1d242d, 0x262c36,   /* base, surface, surface-hi, hairline */
    0x58a6ff, 0xb48cff, 0xf85149, 0xf0883e, 0x3fb950, 0xd29922,   /* accent, violet, red, orange, green, yellow */
    0xe6edf3, 0xc9d1d9, 0x8b949e, 0x545d68, 0x6e7681,   /* text-hi, text-2, muted, text-lo, idle */
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
    lv_style_set_bg_color(&cb_st_panel, cb_col(CB_SURFACE));
    lv_style_set_bg_opa(&cb_st_panel, LV_OPA_COVER);
    lv_style_set_border_color(&cb_st_panel, cb_col(CB_HAIRLINE));
    lv_style_set_border_opa(&cb_st_panel, LV_OPA_COVER);
    lv_style_set_border_width(&cb_st_panel, 1);
    lv_style_set_radius(&cb_st_panel, 14);
    lv_style_set_pad_hor(&cb_st_panel, 18);
    lv_style_set_pad_ver(&cb_st_panel, 16);
    lv_style_set_pad_row(&cb_st_panel, 12);
    lv_style_set_text_color(&cb_st_panel, cb_col(CB_TEXT_HI));
    lv_style_set_text_font(&cb_st_panel, cb_font(CB_F_BODY));

    lv_style_init(&cb_st_card);
    lv_style_set_bg_color(&cb_st_card, cb_col(CB_SURFACE));
    lv_style_set_bg_opa(&cb_st_card, LV_OPA_COVER);
    lv_style_set_border_color(&cb_st_card, cb_col(CB_HAIRLINE));
    lv_style_set_border_opa(&cb_st_card, LV_OPA_COVER);
    lv_style_set_border_width(&cb_st_card, 1);
    lv_style_set_radius(&cb_st_card, 14);
    lv_style_set_pad_hor(&cb_st_card, 14);
    lv_style_set_pad_ver(&cb_st_card, 12);
    lv_style_set_pad_row(&cb_st_card, 8);

    lv_style_init(&cb_st_chip);
    lv_style_set_bg_color(&cb_st_chip, cb_col(CB_SURFACE_HI));
    lv_style_set_bg_opa(&cb_st_chip, LV_OPA_COVER);
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

