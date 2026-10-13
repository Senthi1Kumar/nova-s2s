#include "live/nav_state.h"
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#define DOT " \xC2\xB7 "

static size_t done(int n, size_t cap) { return n < 0 ? 0 : (size_t)n >= cap ? cap - 1 : (size_t)n; }

nav_maneuver_t nav_maneuver(const char *type, const char *modifier)
{
    const char *t = type ? type : "";
    if(!strcmp(t, "arrive")) return NAV_M_ARRIVE;
    if(!strcmp(t, "depart")) return NAV_M_DEPART;
    if(strstr(t, "roundabout") || strstr(t, "rotary")) return NAV_M_ROUNDABOUT;
    static const struct { const char *m; nav_maneuver_t k; } k_mod[] = {
        { "uturn", NAV_M_UTURN },         { "sharp right", NAV_M_SHARP_RIGHT }, { "slight right", NAV_M_SLIGHT_RIGHT },
        { "right", NAV_M_RIGHT },         { "sharp left", NAV_M_SHARP_LEFT },   { "slight left", NAV_M_SLIGHT_LEFT },
        { "left", NAV_M_LEFT },
    };
    const char *m = modifier ? modifier : "";
    for(size_t i = 0; i < sizeof k_mod / sizeof k_mod[0]; i++)
        if(!strcmp(m, k_mod[i].m)) return k_mod[i].k;
    return NAV_M_STRAIGHT;
}

/* Rounded the way a driver reads a sign: 10 m steps up close, 50 m further out, one decimal up to 10 km. */
size_t nav_fmt_distance(char *out, size_t cap, double meters, bool imperial)
{
    double m = meters > 0 ? meters : 0;
    if(imperial) {
        double mi = m / 1609.344;
        if(mi < 0.1) {
            long ft = lround(m * 3.28084 / 50.0) * 50;
            return done(snprintf(out, cap, "%ld ft", ft < 50 ? 50 : ft), cap);
        }
        return done(snprintf(out, cap, mi < 10 ? "%.1f mi" : "%.0f mi", mi), cap);
    }
    if(m < 1000) {
        long r = m < 100 ? lround(m / 10.0) * 10 : lround(m / 50.0) * 50;
        if(r >= 1000) return done(snprintf(out, cap, "1.0 km"), cap);
        return done(snprintf(out, cap, "%ld m", r < 10 ? 10 : r), cap);
    }
    return done(snprintf(out, cap, m < 10000 ? "%.1f km" : "%.0f km", m / 1000.0), cap);
}

size_t nav_fmt_duration(char *out, size_t cap, double seconds)
{
    long t = seconds > 0 ? lround(seconds) : 0;
    if(t < 60) return done(snprintf(out, cap, "%ld s", t), cap);
    if(t < 3600)
        return done(t % 60 ? snprintf(out, cap, "%ld min %ld s", t / 60, t % 60) : snprintf(out, cap, "%ld min", t / 60),
                    cap);
    long mins = (t + 30) / 60;
    return done(snprintf(out, cap, "%ld h %02ld min", mins / 60, mins % 60), cap);
}

size_t nav_fmt_clock(char *out, size_t cap, long epoch_s)
{
    time_t t = (time_t)epoch_s;
    struct tm tmv;
    localtime_r(&t, &tmv);
    return done(snprintf(out, cap, "%02d:%02d", tmv.tm_hour, tmv.tm_min), cap);
}

void nav_state_from_frame(nav_state_t *s, const cbj_t *f)
{
    s->active = cbj_bool(cbj_get(f, "active"), false);
    s->has_progress = false;
    s->step = 0;
    s->to_maneuver_m = s->remaining_s = s->remaining_m = 0;
    s->n_steps = 0;
    s->duration_s = s->distance_m = 0;
    s->arrival_epoch = 0;
    if(!s->active) {
        s->dest[0] = '\0';
        s->eta_min = s->distance_km = 0;
        return;
    }
    snprintf(s->dest, sizeof s->dest, "%s", cbj_str(cbj_get(cbj_get(f, "destination"), "name"), "Destination"));
    s->eta_min = cbj_num(cbj_get(f, "eta_min"), 0);
    s->distance_km = cbj_num(cbj_get(f, "distance_km"), 0);
    s->duration_s = cbj_num(cbj_get(f, "duration_s"), 0);
    s->distance_m = cbj_num(cbj_get(f, "distance_m"), 0);
    s->arrival_epoch = (long)cbj_num(cbj_get(f, "arrival_epoch"), 0);
    const cbj_t *steps = cbj_get(f, "steps");
    for(const cbj_t *st = steps ? steps->child : NULL; st && s->n_steps < NAV_STEPS_MAX; st = st->next) {
        nav_step_t *p = &s->steps[s->n_steps++];
        p->kind = nav_maneuver(cbj_str(cbj_get(st, "type"), NULL), cbj_str(cbj_get(st, "modifier"), NULL));
        snprintf(p->instruction, sizeof p->instruction, "%s", cbj_str(cbj_get(st, "instruction"), ""));
        snprintf(p->street, sizeof p->street, "%s", cbj_str(cbj_get(st, "street"), ""));
        p->distance_m = cbj_num(cbj_get(st, "distance_m"), 0);
    }
}

void nav_state_progress(nav_state_t *s, const cbj_t *f)
{
    s->has_progress = true;
    int k = (int)cbj_num(cbj_get(f, "step"), s->step);
    s->step = k < 0 ? 0 : k;
    s->to_maneuver_m = cbj_num(cbj_get(f, "to_maneuver_m"), s->to_maneuver_m);
    s->remaining_s = cbj_num(cbj_get(f, "remaining_s"), s->remaining_s);
    s->remaining_m = cbj_num(cbj_get(f, "remaining_m"), s->remaining_m);
}

void nav_banner(const nav_state_t *s, bool imperial, long now_epoch, nav_banner_t *b)
{
    memset(b, 0, sizeof *b);
    snprintf(b->dest, sizeof b->dest, "%s", s->dest);
    if(s->n_steps > 0) {   /* before the first progress frame the next maneuver follows the departure step */
        int i = s->has_progress ? s->step : s->n_steps > 1 ? 1 : 0;
        if(i >= s->n_steps) i = s->n_steps - 1;
        b->turn = true;
        b->kind = s->steps[i].kind;
        snprintf(b->instruction, sizeof b->instruction, "%s", s->steps[i].instruction);
        nav_fmt_distance(b->turn_dist, sizeof b->turn_dist, s->has_progress ? s->to_maneuver_m : s->steps[0].distance_m,
                         imperial);
    }
    double left_s = s->has_progress ? s->remaining_s : s->duration_s > 0 ? s->duration_s : s->eta_min * 60.0;
    double left_m = s->has_progress ? s->remaining_m : s->distance_m > 0 ? s->distance_m : s->distance_km * 1000.0;
    char t[32], dist[48], clk[16];
    nav_fmt_duration(t, sizeof t, left_s);
    double km = left_m / 1000.0, mi = left_m / 1609.344;
    if(imperial) snprintf(dist, sizeof dist, "%.1f mi (%.1f km)", mi, km);
    else snprintf(dist, sizeof dist, "%.1f km (%.1f mi)", km, mi);
    long arrive = now_epoch > 0 ? now_epoch + lround(left_s) : s->arrival_epoch;
    if(arrive > 0) {
        nav_fmt_clock(clk, sizeof clk, arrive);
        snprintf(b->trip, sizeof b->trip, "%s" DOT "%s" DOT "arrive %s", t, dist, clk);
    }
    else snprintf(b->trip, sizeof b->trip, "%s" DOT "%s", t, dist);
}
