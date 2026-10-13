#pragma once
#include "live/json.h"
#include <stdbool.h>
#include <stddef.h>

/* Turn-by-turn state from nova-hailo's nova.nav (route, Mapbox steps, trip totals) and nova.nav.progress (the
   next maneuver and what is left), and the banner's words. Pure C: no LVGL. An older pipeline that sends no
   steps still gets the destination and a trip line from eta_min / distance_km. */

typedef enum {
    NAV_M_STRAIGHT, NAV_M_SLIGHT_LEFT, NAV_M_LEFT, NAV_M_SHARP_LEFT, NAV_M_UTURN,
    NAV_M_SLIGHT_RIGHT, NAV_M_RIGHT, NAV_M_SHARP_RIGHT, NAV_M_ROUNDABOUT, NAV_M_DEPART, NAV_M_ARRIVE, NAV_M_COUNT
} nav_maneuver_t;

#define NAV_STEPS_MAX 80

typedef struct {
    nav_maneuver_t kind;
    char instruction[128], street[64];
    double distance_m;   /* the step's length: from this maneuver to the next */
} nav_step_t;

typedef struct {
    bool active;
    char dest[96];
    double eta_min, distance_km;     /* also moved by the 1 Hz vehicle deltas */
    double duration_s, distance_m;   /* the route's totals from Directions, 0 when absent */
    long arrival_epoch;
    int n_steps;
    nav_step_t steps[NAV_STEPS_MAX];
    bool has_progress;
    int step;                         /* index of the next maneuver */
    double to_maneuver_m, remaining_s, remaining_m;
} nav_state_t;

typedef struct {
    bool turn;                       /* a maneuver to show (steps are known) */
    nav_maneuver_t kind;
    char turn_dist[24], instruction[128], dest[96];
    char trip[160];                  /* "37 min 11 s · 21.0 km (13.0 mi) · arrive 20:41" */
} nav_banner_t;

nav_maneuver_t nav_maneuver(const char *type, const char *modifier);   /* Mapbox step maneuver */
size_t nav_fmt_distance(char *out, size_t cap, double meters, bool imperial);   /* "250 m", "1.2 km", "400 ft" */
size_t nav_fmt_duration(char *out, size_t cap, double seconds);                 /* "45 s", "37 min 11 s", "1 h 05 min" */
size_t nav_fmt_clock(char *out, size_t cap, long epoch_s);                      /* local "20:41" */

void nav_state_from_frame(nav_state_t *s, const cbj_t *frame);   /* nova.nav: replaces the route, resets progress */
void nav_state_progress(nav_state_t *s, const cbj_t *frame);     /* nova.nav.progress */
/* now_epoch > 0 dates the arrival from what is left; 0 uses the route's own arrival time. */
void nav_banner(const nav_state_t *s, bool imperial, long now_epoch, nav_banner_t *out);
