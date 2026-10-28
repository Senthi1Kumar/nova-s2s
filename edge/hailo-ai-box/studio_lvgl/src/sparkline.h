#pragma once
#include "lvgl.h"
#include "theme.h"

/* Line chart with a soft area fill under the line (a 2-stop vertical gradient per segment, so it
   renders the same on the software and NanoVG renderers). New values shift in from the right. */
lv_obj_t *sparkline_create(lv_obj_t *parent, int32_t w, int32_t h, uint32_t points, cb_color_t color,
                           int32_t min, int32_t max);
void sparkline_push(lv_obj_t *spark, int32_t value);
