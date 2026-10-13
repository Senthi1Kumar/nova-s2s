#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Touch gestures for the map page, as Mapbox GL JS feels them: the map follows the finger one move at a time,
   a release while still moving glides on (inertia: max 1400 px/s, deceleration 2500 px/s^2, linearity 0.3,
   velocity from the last 80 ms), and two taps within 300 ms and 30 px zoom in. Pure; times in ms. */
#define CB_DRAG_HIST 16

typedef struct {
    int32_t x, y;
    uint32_t t;
} cb_drag_pt_t;

typedef struct {
    cb_drag_pt_t hist[CB_DRAG_HIST];
    int head, n;
    int32_t last_x, last_y;
    bool tap_armed;
    int32_t tap_x, tap_y;
    uint32_t tap_t;
} cb_drag_t;   /* zero-initialise once; begin() keeps the tap state across presses */

void cb_drag_begin(cb_drag_t *d, int32_t x, int32_t y, uint32_t t);
void cb_drag_move(cb_drag_t *d, int32_t x, int32_t y, uint32_t t, int32_t *dx, int32_t *dy);
bool cb_drag_end(cb_drag_t *d, uint32_t t, double *fling_x, double *fling_y, double *fling_ms);   /* false: no glide */
bool cb_tap_is_double(cb_drag_t *d, int32_t x, int32_t y, uint32_t t);
