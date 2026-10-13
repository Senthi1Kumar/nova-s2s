#pragma once
#include "lvgl.h"

/* Colour tokens from hmi_qt/NovaUI/Theme.qml plus the prototype's text-2 and orb-idle greys. */
typedef enum {
    CB_BASE, CB_SURFACE, CB_SURFACE_HI, CB_HAIRLINE,
    CB_CYAN, CB_VIOLET, CB_RED, CB_ORANGE, CB_GREEN, CB_YELLOW,
    CB_TEXT_HI, CB_TEXT_2, CB_TEXT_MID, CB_TEXT_LO, CB_ORB_IDLE,
    CB_COLOR_COUNT
} cb_color_t;

/* Font roles: Inter 11 12 14 16 20 34 52 | JetBrains Mono 11 12 14. */
typedef enum {
    CB_F_SMALL, CB_F_LABEL, CB_F_BODY, CB_F_VALUE, CB_F_TITLE, CB_F_TEMP, CB_F_SPEED,
    CB_F_MONO_S, CB_F_MONO, CB_F_MONO_L,
    CB_FONT_COUNT
} cb_font_t;

#define CB_PANEL_W 1920
#define CB_PANEL_H 720

void cb_theme_init(void);

/* Blacks out `screen` and centres a CB_PANEL_W x CB_PANEL_H car panel on it (stays centred on resize). */
lv_obj_t *cb_panel_create(lv_obj_t *screen);
lv_color_t cb_col(cb_color_t c);
const lv_font_t *cb_font(cb_font_t f);

/* UI-wide motion switch (--reduce-motion): ambient loops stay off and transitions jump to their end state. */
void cb_theme_set_reduce_motion(bool on);
bool cb_reduce_motion(void);

/* Shared styles, valid after cb_theme_init(). */
extern lv_style_t cb_st_panel;   /* glass column: white bg opa 11, 1 px white border opa 23, radius 20, pad 16/18, row gap 12 */
extern lv_style_t cb_st_card;    /* inner card: white bg opa 9, radius 14, pad 12/14, row gap 8, no border */
extern lv_style_t cb_st_chip;    /* pill: white bg opa 15, radius circle, pad 3/10, CB_F_LABEL, CB_TEXT_2 */
extern lv_style_t cb_st_label;   /* uppercase section label: CB_F_LABEL, letter space 2, CB_TEXT_MID */
