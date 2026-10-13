#pragma once
#include "lvgl.h"

/* Renderer gate: every drawing primitive the dashboard uses, one per cell, all animated, so `gpu`
   (NanoVG) and `sw` can be compared side by side and measured on the Pi. */
void gate_screen_create(lv_obj_t *panel);
