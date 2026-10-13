#pragma once
#include "lvgl.h"
#include "cb_theme.h"

/* Small builders shared by the dashboard: every container is style-less, non-scrollable and
   non-clickable unless a caller opts in, so nothing inherits LVGL defaults by accident. */
lv_obj_t *ui_plain(lv_obj_t *parent);
lv_obj_t *ui_panel(lv_obj_t *parent, const char *title);   /* glass column with header (title + SIM badge) */
lv_obj_t *ui_card(lv_obj_t *parent);                       /* inner card, flex column, width 100 % */
lv_obj_t *ui_row(lv_obj_t *parent, int32_t gap);           /* flex row, cross-centre, width 100 % */
lv_obj_t *ui_col(lv_obj_t *parent, int32_t gap);           /* flex column, width 100 % */
lv_obj_t *ui_spacer(lv_obj_t *row);                        /* flex_grow 1 */
lv_obj_t *ui_label(lv_obj_t *parent, const char *text, cb_font_t f, cb_color_t c);
lv_obj_t *ui_label_line(lv_obj_t *parent, const char *text, cb_font_t f, cb_color_t c);   /* width 100 %, one line, dots */
lv_obj_t *ui_dot(lv_obj_t *parent, cb_color_t c, bool breathing);   /* 6 px; breathing = 2.4 s halo */
lv_obj_t *ui_chip(lv_obj_t *parent, const char *text, cb_color_t dot, bool breathing);
lv_obj_t *ui_chip_label(lv_obj_t *chip);
lv_obj_t *ui_badge_sim(lv_obj_t *parent);
