#pragma once
#include "lvgl.h"

/* Frame D on the 1920x720 panel: status strip (y 14..58), three glass columns Drive / Comfort / Connected
   (y 70..630, 616 px wide, 16 px gaps), dock (y 636..708) with quick chips, the Nova bar slot (760 wide,
   centred) and the frame buttons. Columns are returned empty; Tasks 8-10 fill them. */
typedef struct {
    lv_obj_t *drive, *comfort, *connected;
    lv_obj_t *dock_mid;    /* 760x72 slot the Nova bar is centred in */
    lv_obj_t *pause_btn;   /* "Pause demo" chip; cb_app wires the click */
} dash_t;

dash_t dash_create(lv_obj_t *panel);
