#include "map/map_gesture.h"
#include <math.h>
#include <stdlib.h>

#define WINDOW_MS 80
#define STOP_MS 60          /* a finger resting this long before release does not fling */
#define MIN_SPEED 250.0     /* px/s */
#define MAX_SPEED 1400.0
#define DECELERATION 2500.0
#define LINEARITY 0.3
#define TAP_MS 300
#define TAP_PX 30

static void push(cb_drag_t *d, int32_t x, int32_t y, uint32_t t)
{
    d->hist[d->head] = (cb_drag_pt_t){ x, y, t };
    d->head = (d->head + 1) % CB_DRAG_HIST;
    if(d->n < CB_DRAG_HIST) d->n++;
}

static const cb_drag_pt_t *at(const cb_drag_t *d, int back)   /* 0 = newest */
{
    return &d->hist[(d->head - 1 - back + 2 * CB_DRAG_HIST) % CB_DRAG_HIST];
}

void cb_drag_begin(cb_drag_t *d, int32_t x, int32_t y, uint32_t t)
{
    d->head = d->n = 0;
    d->last_x = x;
    d->last_y = y;
    push(d, x, y, t);
}

void cb_drag_move(cb_drag_t *d, int32_t x, int32_t y, uint32_t t, int32_t *dx, int32_t *dy)
{
    *dx = x - d->last_x;
    *dy = y - d->last_y;
    d->last_x = x;
    d->last_y = y;
    push(d, x, y, t);
}

bool cb_drag_end(cb_drag_t *d, uint32_t t, double *fx, double *fy, double *ms)
{
    if(d->n < 2) return false;
    const cb_drag_pt_t *last = at(d, 0), *first = last;
    if(t - last->t > STOP_MS) return false;
    for(int i = 1; i < d->n; i++) {
        const cb_drag_pt_t *p = at(d, i);
        if(last->t - p->t > WINDOW_MS) break;
        first = p;
    }
    double dt = (double)(last->t - first->t) / 1000.0;
    if(dt <= 0) return false;
    double vx = (last->x - first->x) / dt, vy = (last->y - first->y) / dt;
    double speed = hypot(vx, vy);
    if(speed < MIN_SPEED) return false;
    if(speed > MAX_SPEED) {
        vx *= MAX_SPEED / speed;
        vy *= MAX_SPEED / speed;
        speed = MAX_SPEED;
    }
    double dur = speed / (DECELERATION * LINEARITY);   /* s */
    *fx = vx * dur / 2;
    *fy = vy * dur / 2;
    *ms = dur * 1000.0;
    return true;
}

bool cb_tap_is_double(cb_drag_t *d, int32_t x, int32_t y, uint32_t t)
{
    if(d->tap_armed && t - d->tap_t <= TAP_MS && abs(x - d->tap_x) <= TAP_PX && abs(y - d->tap_y) <= TAP_PX) {
        d->tap_armed = false;
        return true;
    }
    d->tap_armed = true;
    d->tap_x = x;
    d->tap_y = y;
    d->tap_t = t;
    return false;
}
