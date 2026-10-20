#include "map/map_follow.h"
#include <math.h>
#include <stddef.h>

#define DEG (M_PI / 180.0)
#define PLL_PHASE 0.2
#define PLL_PERIOD 0.01
#define PLL_ACQUIRE 6   /* intervals averaged before the loop takes over */

double map_angle_norm(double deg)
{
    double a = fmod(deg, 360.0);
    return a < 0 ? a + 360.0 : a;
}

static double angle_diff(double a, double b)   /* b - a, in (-180, 180] */
{
    double d = fmod(b - a, 360.0);
    if(d > 180.0) d -= 360.0;
    if(d <= -180.0) d += 360.0;
    return d;
}

double map_angle_lerp(double a, double b, double u) { return map_angle_norm(a + angle_diff(a, b) * u); }

double map_approach(double cur, double target, double dt_ms, double tau_ms)
{
    if(tau_ms <= 0 || dt_ms <= 0) return dt_ms > 0 ? target : cur;
    return target + (cur - target) * exp(-dt_ms / tau_ms);
}

double map_angle_approach(double cur, double target, double dt_ms, double tau_ms)
{
    if(dt_ms <= 0) return map_angle_norm(cur);
    double keep = tau_ms > 0 ? exp(-dt_ms / tau_ms) : 0;
    return map_angle_norm(target - angle_diff(cur, target) * keep);
}

double map_smoothstep(double u)
{
    u = u < 0 ? 0 : u > 1 ? 1 : u;
    return u * u * (3 - 2 * u);
}

double map_distance_m(double lat1, double lon1, double lat2, double lon2)
{
    double p1 = lat1 * DEG, p2 = lat2 * DEG, dl = (lon2 - lon1) * DEG;
    double h = sin((p2 - p1) / 2) * sin((p2 - p1) / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2);
    return 2 * 6371000.0 * asin(sqrt(h));
}

void map_track_push(map_track_t *t, double lat, double lon, double heading, double t_ms)
{
    map_sample_t s = { lat, lon, map_angle_norm(heading), t_ms };
    const map_sample_t *last = t->n ? &t->s[t->n - 1] : NULL;
    if(!last || t_ms <= last->t_ms || map_distance_m(last->lat, last->lon, lat, lon) > MAP_TRACK_JUMP_M) {
        t->s[0] = s;   /* start over: a jump, or time going backwards, is nothing to interpolate across */
        t->n = 1;
        t->interval = t->delay = 0;
        t->misses = t->acquired = 0;
        t->acq_t0 = t_ms;
        return;
    }
    /* Samples are evenly spaced at the source but stamped on arrival, so arrival jitter would read as the car
       speeding up and slowing down. First the rate is acquired: a few samples stamped as they came, the interval
       their average. Then a phase-locked loop stamps each sample where that rate predicts it, nudged toward when
       it really came (phase), and lets the rate drift slowly (period): +-20 ms of jitter at 10 Hz drops from
       ~22 % speed wobble to a few percent. Two arrivals in a row far off the rate are a new rate (the sim handing
       over to a 1 Hz GPS), not jitter: it is acquired again. */
    if(t->acquired >= PLL_ACQUIRE) {
        double band = 0.4 * t->interval, err = t_ms - (last->t_ms + t->interval);
        t->misses = fabs(err) > band ? t->misses + 1 : 0;
        if(t->misses >= 2) {
            t->acquired = t->misses = 0;
            t->acq_t0 = last->t_ms;
        }
        else {
            err = err > band ? band : err < -band ? -band : err;   /* one outlier moves the loop no further */
            s.t_ms = last->t_ms + t->interval + err * PLL_PHASE;
            t->interval += err * PLL_PERIOD;
        }
    }
    if(t->acquired < PLL_ACQUIRE) {
        t->acquired++;
        t->interval = (t_ms - t->acq_t0) / t->acquired;
    }
    /* One and a half intervals behind: the extra half is a jitter buffer, so a sample arriving a little late
       never leaves the car held at the last one. The delay eases rather than jumps, or each refined estimate
       would nudge the drawn car backwards; faster while acquiring, so a new rate settles in a few samples. */
    double target = 1.5 * (t->interval < 40 ? 40 : t->interval > 1000 ? 1000 : t->interval);
    t->delay = t->delay > 0 ? t->delay + (target - t->delay) * (t->acquired < PLL_ACQUIRE ? 0.3 : 0.1) : target;
    if(t->n == MAP_TRACK_N) {
        for(int i = 1; i < MAP_TRACK_N; i++) t->s[i - 1] = t->s[i];
        t->n--;
    }
    t->s[t->n++] = s;
}

bool map_track_at(const map_track_t *t, double now_ms, double *lat, double *lon, double *heading)
{
    if(t->n == 0) return false;
    const map_sample_t *a = &t->s[t->n - 1], *b = a;
    double u = 0;
    if(t->n >= 2) {
        double td = now_ms - t->delay;
        if(td <= t->s[0].t_ms) a = b = &t->s[0];
        else if(td < t->s[t->n - 1].t_ms) {
            int i = 0;
            while(i + 2 < t->n && t->s[i + 1].t_ms <= td) i++;
            a = &t->s[i];
            b = &t->s[i + 1];
            u = (td - a->t_ms) / (b->t_ms - a->t_ms);
        }
    }
    *lat = a->lat + (b->lat - a->lat) * u;
    *lon = a->lon + (b->lon - a->lon) * u;
    *heading = map_angle_lerp(a->heading, b->heading, u);
    return true;
}
