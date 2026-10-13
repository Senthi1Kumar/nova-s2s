#include "map/map_gesture.h"
#include <math.h>
#include <stdio.h>

static int fails;
#define CHECK(c) do { if(!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while(0)
#define NEAR(a, b, tol) CHECK(fabs((double)(a) - (double)(b)) <= (tol))

static void drag_deltas(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    cb_drag_begin(&d, 100, 100, 0);
    cb_drag_move(&d, 110, 104, 16, &dx, &dy);
    CHECK(dx == 10 && dy == 4);
    cb_drag_move(&d, 115, 104, 32, &dx, &dy);
    CHECK(dx == 5 && dy == 0);
}

static void fling_follows_release_velocity(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    double fx, fy, ms;
    cb_drag_begin(&d, 0, 0, 0);
    for(int i = 1; i <= 5; i++) cb_drag_move(&d, 20 * i, 0, (uint32_t)(16 * i), &dx, &dy);
    CHECK(cb_drag_end(&d, 90, &fx, &fy, &ms));
    /* 100 px over 80 ms = 1250 px/s -> duration 1250 / (2500 * 0.3) s, offset v * duration / 2 */
    NEAR(ms, 1666.7, 1.0);
    NEAR(fx, 1041.7, 1.0);
    NEAR(fy, 0, 1e-9);
}

static void no_fling_after_a_stop(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    double fx, fy, ms;
    cb_drag_begin(&d, 0, 0, 0);
    for(int i = 1; i <= 5; i++) cb_drag_move(&d, 20 * i, 0, (uint32_t)(16 * i), &dx, &dy);
    CHECK(!cb_drag_end(&d, 300, &fx, &fy, &ms));
}

static void no_fling_when_slow(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    double fx, fy, ms;
    cb_drag_begin(&d, 0, 0, 0);
    for(int i = 1; i <= 5; i++) cb_drag_move(&d, 2 * i, 0, (uint32_t)(16 * i), &dx, &dy);
    CHECK(!cb_drag_end(&d, 85, &fx, &fy, &ms));
}

static void fling_speed_is_capped(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    double fx, fy, ms;
    cb_drag_begin(&d, 0, 0, 0);
    for(int i = 1; i <= 5; i++) cb_drag_move(&d, 0, -100 * i, (uint32_t)(16 * i), &dx, &dy);
    CHECK(cb_drag_end(&d, 82, &fx, &fy, &ms));
    NEAR(ms, 1866.7, 1.0);    /* 1400 / 750 s */
    NEAR(fy, -1306.7, 1.0);   /* 1400 * 0.9333 */
    NEAR(fx, 0, 1e-9);
}

static void old_samples_do_not_count(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    int32_t dx, dy;
    double fx, fy, ms;
    cb_drag_begin(&d, 0, 0, 0);
    cb_drag_move(&d, 400, 0, 16, &dx, &dy);    /* a fast start ... */
    cb_drag_move(&d, 400, 0, 200, &dx, &dy);   /* ... then a long hold */
    cb_drag_move(&d, 402, 0, 216, &dx, &dy);
    CHECK(!cb_drag_end(&d, 220, &fx, &fy, &ms));
}

static void double_tap(void)
{
    cb_drag_t d = { 0 };   /* begin() keeps the tap state across presses */
    cb_drag_begin(&d, 0, 0, 0);
    CHECK(!cb_tap_is_double(&d, 10, 10, 1000));
    CHECK(cb_tap_is_double(&d, 14, 12, 1200));
    CHECK(!cb_tap_is_double(&d, 14, 12, 1400));   /* a third tap starts over */
    CHECK(!cb_tap_is_double(&d, 100, 100, 2000));
    CHECK(!cb_tap_is_double(&d, 10, 10, 2100));    /* too far from the first */
    CHECK(!cb_tap_is_double(&d, 10, 10, 2600));    /* too late */
}

int main(void)
{
    drag_deltas();
    fling_follows_release_velocity();
    no_fling_after_a_stop();
    no_fling_when_slow();
    fling_speed_is_capped();
    old_samples_do_not_count();
    double_tap();
    if(fails) return 1;
    printf("ok\n");
    return 0;
}
