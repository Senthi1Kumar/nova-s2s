#pragma once
#include "lvgl.h"

/* Comfort column contents (frame D): climate card (temps, pulsing fan bars, A/C chip), 12 device tiles in a
   4x3 grid with mini-graphics that flash cyan when their subject changes, and the compact media player.
   Everything observes cb_model subjects; the phone card lives in the Connected column. */
void comfort_build(lv_obj_t *column);
void comfort_build_parts(lv_obj_t *climate, lv_obj_t *tiles, lv_obj_t *media);   /* layout B: cards in separate parents */

/* How a tile marks a change: TINT (cyan border and wash, 2.2 s; frame D and layout B) or GRADIENT (a border whose
   cyan -> violet -> pink gradient travels around the tile, with a matching glow, 1.5 s; layout v2). Set before
   the tiles are built. */
typedef enum { COMFORT_FLASH_TINT, COMFORT_FLASH_GRADIENT } comfort_flash_t;
void comfort_set_flash_style(comfort_flash_t style);
