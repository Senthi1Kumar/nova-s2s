#pragma once
#include <stdbool.h>

/* The car as the map draws it, between position samples. Samples arrive at the sim's rate (5-10 Hz); drawing the
   car one and a half sample intervals behind real time (the extra half a jitter buffer) and interpolating between
   samples keeps it moving every frame instead of hopping. The delay is a smoothed interval, held steady across arrivals: taking it from the last two arrival
   times alone made the car surge and slow at each sample when they came unevenly. Pure math, no MapLibre: the
   map worker calls it once per rendered frame. */
typedef struct {
    double lat, lon, heading, t_ms;
} map_sample_t;

#define MAP_TRACK_N 4

typedef struct {
    map_sample_t s[MAP_TRACK_N];   /* oldest first */
    int n;
    double interval;   /* smoothed time between samples, ms */
    int misses;        /* arrivals in a row far from the steady rate */
    int acquired;      /* intervals measured since the rate was (re)acquired */
    double delay;      /* how far behind real time the car is drawn, ms: eases toward 1.5 intervals */
    double acq_t0;     /* arrival that started the acquisition */
} map_track_t;

/* A sample farther than this from the last one is a jump (a new trip, a GPS fix appearing), not motion. */
#define MAP_TRACK_JUMP_M 300.0

void map_track_push(map_track_t *t, double lat, double lon, double heading, double t_ms);
/* The car at now_ms, 1.5 smoothed intervals behind; the heading turns the short way round. false before any
   sample. A late sample holds the car at the last one: it never runs ahead of the data. */
bool map_track_at(const map_track_t *t, double now_ms, double *lat, double *lon, double *heading);

double map_angle_norm(double deg);                            /* [0, 360) */
double map_angle_lerp(double a, double b, double u);          /* degrees, the short way round, u in 0..1 */
double map_approach(double cur, double target, double dt_ms, double tau_ms);         /* exponential easing */
double map_angle_approach(double cur, double target, double dt_ms, double tau_ms);   /* the same, for degrees */
double map_smoothstep(double u);                              /* 0..1, eased at both ends; u clamped */
double map_distance_m(double lat1, double lon1, double lat2, double lon2);
