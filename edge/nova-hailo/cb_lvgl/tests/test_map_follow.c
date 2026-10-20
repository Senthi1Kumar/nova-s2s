#include "cb_test.h"
#include "map/map_follow.h"
#include <math.h>

static bool near(double a, double b, double eps) { return fabs(a - b) <= eps; }

/* 5 Hz samples, 200 ms apart: the car is drawn 1.5 intervals (300 ms) behind and moves every frame between them. */
static void moves_between_samples(void)
{
    map_track_t t = { 0 };
    double lat, lon, h;
    CHECK(!map_track_at(&t, 0, &lat, &lon, &h));
    map_track_push(&t, 37.0000, -122.0000, 0, 1000);
    CHECK(map_track_at(&t, 1100, &lat, &lon, &h) && lat == 37.0000);    /* one sample: held there */
    map_track_push(&t, 37.0001, -122.0000, 10, 1200);
    CHECK(map_track_at(&t, 1300, &lat, &lon, &h) && near(lat, 37.0000, 1e-9));    /* now - 300 ms = first sample */
    CHECK(map_track_at(&t, 1400, &lat, &lon, &h) && near(lat, 37.00005, 1e-9) && near(h, 5, 1e-9));   /* halfway */
    CHECK(map_track_at(&t, 1500, &lat, &lon, &h) && near(lat, 37.0001, 1e-9));
    CHECK(map_track_at(&t, 1900, &lat, &lon, &h) && near(lat, 37.0001, 1e-9));   /* late sample: held, not run past */
    double l1, l2;
    map_track_at(&t, 1350, &l1, &lon, &h);
    map_track_at(&t, 1366, &l2, &lon, &h);
    CHECK(l2 > l1);   /* a 60 fps frame later, the car has moved */
}

/* Samples arriving unevenly (180, then 220 ms) must not make the car surge at each arrival: what is drawn just
   before a sample lands and just after it differ by a sliver, not by a step. */
static void uneven_arrivals_stay_smooth(void)
{
    map_track_t t = { 0 };
    double lat0, lat1, lon, h;
    const double times[] = { 0, 180, 400, 580, 800, 980 };
    for(int k = 0; k < 6; k++) map_track_push(&t, 37.0 + k * 0.0001, -122.0, 0, times[k]);
    map_track_at(&t, 1199, &lat0, &lon, &h);
    map_track_push(&t, 37.0006, -122.0, 0, 1200);
    map_track_at(&t, 1200, &lat1, &lon, &h);
    CHECK(fabs(lat1 - lat0) < 0.00002);   /* under a fifth of a sample's step */
    CHECK(lat1 > lat0 - 1e-7);            /* never visibly backwards: under 1 cm, far below a pixel */
}

/* 10 Hz samples with +-20 ms arrival jitter, drawn at 60 fps as they arrive: the drawn speed wobbles far less
   than the arrival times do. */
static double speed_wobble(bool jitter)
{
    double arrive[60];
    unsigned seed = 7;
    for(int k = 0; k < 60; k++) {
        arrive[k] = k * 100.0;
        if(jitter && k > 0) {
            seed = seed * 1103515245u + 12345u;
            arrive[k] += (double)((seed >> 8) % 41) - 20.0;
        }
    }
    map_track_t t = { 0 };
    double sum = 0, sq = 0, prev_lat = 0, lat, lon, h;
    int n = 0, next = 0;
    for(double now = 0; now < 5800; now += 16.7) {
        while(next < 60 && arrive[next] <= now) {
            map_track_push(&t, 37.0 + next * 0.00001, -122.0, 0, arrive[next]);
            next++;
        }
        if(!map_track_at(&t, now, &lat, &lon, &h)) continue;
        if(now > 1500 && prev_lat != 0) {   /* past the start-up */
            double v = (lat - prev_lat) / 16.7;
            sum += v;
            sq += v * v;
            n++;
        }
        prev_lat = lat;
    }
    double mean = sum / n;
    return sqrt(fmax(sq / n - mean * mean, 0)) / mean;
}

static void arrival_jitter_is_smoothed(void)
{
    CHECK(speed_wobble(false) < 0.01);   /* an even feed is drawn at an even speed */
    CHECK(speed_wobble(true) < 0.05);    /* +-20 ms in 100 ms is ~0.2 stamped raw */
}

/* A real change of rate (5 Hz to 10 Hz) is followed, not smoothed away. */
static void rate_changes_are_followed(void)
{
    map_track_t t = { 0 };
    for(int k = 0; k < 10; k++) map_track_push(&t, 37.0 + k * 1e-5, -122.0, 0, k * 200.0);
    for(int k = 1; k <= 30; k++) map_track_push(&t, 37.0001 + k * 1e-5, -122.0, 0, 1800 + k * 100.0);
    CHECK(t.interval > 95 && t.interval < 110);
    CHECK(fabs(t.s[t.n - 1].t_ms - 4800) < 40);   /* the stamps have caught up with the arrivals */
}

static void jumps_are_not_motion(void)
{
    map_track_t t = { 0 };
    double lat, lon, h;
    map_track_push(&t, 37.0, -122.0, 0, 1000);
    map_track_push(&t, 38.0, -122.0, 0, 1200);   /* 111 km: a new trip, not a drive */
    CHECK(t.n == 1);
    CHECK(map_track_at(&t, 1250, &lat, &lon, &h) && lat == 38.0);
    for(int k = 1; k <= 6; k++) map_track_push(&t, 38.0 + k * 1e-5, -122.0, 0, 1200 + k * 200);
    CHECK(t.n == MAP_TRACK_N);   /* a bounded history */
    map_track_push(&t, 38.0001, -122.0, 0, 1100);   /* an older timestamp starts over too */
    CHECK(t.n == 1);
}

static void headings_turn_the_short_way(void)
{
    CHECK(near(map_angle_lerp(350, 10, 0.5), 0, 1e-9));
    CHECK(near(map_angle_lerp(10, 350, 0.5), 0, 1e-9));
    CHECK(near(map_angle_lerp(90, 270, 0.5), 180, 1e-9));
    CHECK(near(map_angle_norm(-30), 330, 1e-9) && near(map_angle_norm(725), 5, 1e-9));
    map_track_t t = { 0 };
    double lat, lon, h;
    map_track_push(&t, 37.0, -122.0, 359, 0);
    map_track_push(&t, 37.0, -122.0, 1, 200);
    map_track_at(&t, 400, &lat, &lon, &h);
    CHECK(near(h, 0, 1e-9));   /* through north, not all the way round */
}

static void easing(void)
{
    CHECK(near(map_approach(0, 10, 0, 100), 0, 1e-12));
    CHECK(near(map_approach(0, 10, 100, 100), 10 * (1 - exp(-1.0)), 1e-9));
    CHECK(map_approach(0, 10, 1000, 100) > 9.99);
    CHECK(near(map_angle_approach(350, 10, 1e6, 100), 10, 1e-6));
    double a = map_angle_approach(350, 10, 100, 100);   /* partway, through north */
    CHECK(a > 350 || a < 10);
    CHECK(map_smoothstep(-1) == 0 && map_smoothstep(2) == 1 && near(map_smoothstep(0.5), 0.5, 1e-12));
    CHECK(near(map_distance_m(37.0, -122.0, 37.001, -122.0), 111.2, 0.5));
}

int main(void)
{
    RUN(moves_between_samples);
    RUN(uneven_arrivals_stay_smooth);
    RUN(arrival_jitter_is_smoothed);
    RUN(rate_changes_are_followed);
    RUN(jumps_are_not_motion);
    RUN(headings_turn_the_short_way);
    RUN(easing);
    DONE();
}
