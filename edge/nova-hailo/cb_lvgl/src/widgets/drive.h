#pragma once
#include "lvgl.h"

/* Drive column contents (frame D): speed card (eased speed, PRND, speed sparkline, SoC bar + range,
   status chips) and the mini map (grid, road, route, destination, moving position dot, ETA tag).
   Everything observes cb_model subjects. */
void drive_build(lv_obj_t *column);
void drive_build_speed(lv_obj_t *parent);   /* the speed card alone (layout B car pane) */
