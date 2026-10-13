#include "cb_test.h"
#include "live/json.h"
#include "live/nav_state.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>

static nav_state_t s_nav;

static void frame(void (*fn)(nav_state_t *, const cbj_t *), const char *json)
{
    cbj_t *j = cbj_parse(json, strlen(json));
    CHECK(j != NULL);
    if(j) fn(&s_nav, j);
    cbj_free(j);
}

static void maneuvers(void) {
    CHECK_EQ(nav_maneuver("turn", "right"), NAV_M_RIGHT);
    CHECK_EQ(nav_maneuver("turn", "sharp left"), NAV_M_SHARP_LEFT);
    CHECK_EQ(nav_maneuver("fork", "slight right"), NAV_M_SLIGHT_RIGHT);
    CHECK_EQ(nav_maneuver("continue", "uturn"), NAV_M_UTURN);
    CHECK_EQ(nav_maneuver("new name", "straight"), NAV_M_STRAIGHT);
    CHECK_EQ(nav_maneuver("roundabout", "left"), NAV_M_ROUNDABOUT);
    CHECK_EQ(nav_maneuver("exit rotary", "right"), NAV_M_ROUNDABOUT);
    CHECK_EQ(nav_maneuver("arrive", NULL), NAV_M_ARRIVE);
    CHECK_EQ(nav_maneuver("depart", "left"), NAV_M_DEPART);
    CHECK_EQ(nav_maneuver(NULL, NULL), NAV_M_STRAIGHT);
}

static bool fmt_is(size_t (*f)(char *, size_t, double, bool), double v, bool imp, const char *want)
{
    char b[32];
    f(b, sizeof b, v, imp);
    if(strcmp(b, want)) printf("  got \"%s\", want \"%s\"\n", b, want);
    return !strcmp(b, want);
}

static void distances(void) {
    CHECK(fmt_is(nav_fmt_distance, 4, false, "10 m"));
    CHECK(fmt_is(nav_fmt_distance, 64, false, "60 m"));
    CHECK(fmt_is(nav_fmt_distance, 240, false, "250 m"));
    CHECK(fmt_is(nav_fmt_distance, 1234, false, "1.2 km"));
    CHECK(fmt_is(nav_fmt_distance, 15600, false, "16 km"));
    CHECK(fmt_is(nav_fmt_distance, 120, true, "400 ft"));
    CHECK(fmt_is(nav_fmt_distance, 1234, true, "0.8 mi"));
    CHECK(fmt_is(nav_fmt_distance, 21000, true, "13 mi"));
}

static bool dur_is(double s, const char *want)
{
    char b[32];
    nav_fmt_duration(b, sizeof b, s);
    if(strcmp(b, want)) printf("  got \"%s\", want \"%s\"\n", b, want);
    return !strcmp(b, want);
}

static void durations(void) {
    CHECK(dur_is(45, "45 s"));
    CHECK(dur_is(2231, "37 min 11 s"));   /* the Directions playground's "37 minutes, 11 seconds" */
    CHECK(dur_is(480, "8 min"));
    CHECK(dur_is(3900, "1 h 05 min"));
    CHECK(dur_is(-3, "0 s"));
}

static void clock_text(void) {
    setenv("TZ", "UTC", 1);
    tzset();
    char b[16];
    nav_fmt_clock(b, sizeof b, 20 * 3600 + 41 * 60);
    CHECK(!strcmp(b, "20:41"));
}

static const char *k_nav =
    "{\"type\":\"nova.nav\",\"active\":true,\"destination\":{\"name\":\"Los Altos Hills\",\"lat\":37.37,\"lon\":-122.13},"
    "\"eta_min\":37.2,\"distance_km\":21.0,\"duration_s\":2231,\"distance_m\":21000,\"arrival_epoch\":74460,"
    "\"steps\":[{\"type\":\"depart\",\"modifier\":\"left\",\"instruction\":\"Drive south on Warren Street\","
    "\"street\":\"Warren Street\",\"distance_m\":350},"
    "{\"type\":\"turn\",\"modifier\":\"right\",\"instruction\":\"Turn right onto Broadway\",\"street\":\"Broadway\","
    "\"distance_m\":1800},"
    "{\"type\":\"arrive\",\"instruction\":\"You have arrived at Los Altos Hills\",\"distance_m\":0}]}";

static void banner_from_steps(void) {
    memset(&s_nav, 0, sizeof s_nav);
    frame(nav_state_from_frame, k_nav);
    CHECK(s_nav.active);
    CHECK_EQ(s_nav.n_steps, 3);
    nav_banner_t b;
    nav_banner(&s_nav, false, 0, &b);   /* no progress yet: the first turn after the departure step */
    CHECK(b.turn);
    CHECK_EQ(b.kind, NAV_M_RIGHT);
    CHECK(!strcmp(b.turn_dist, "350 m"));
    CHECK(!strcmp(b.instruction, "Turn right onto Broadway"));
    CHECK(!strcmp(b.dest, "Los Altos Hills"));
    if(strcmp(b.trip, "37 min 11 s \xC2\xB7 21.0 km (13.0 mi) \xC2\xB7 arrive 20:41")) printf("  trip \"%s\"\n", b.trip);
    CHECK(!strcmp(b.trip, "37 min 11 s \xC2\xB7 21.0 km (13.0 mi) \xC2\xB7 arrive 20:41"));
}

static void progress_moves_on(void) {
    frame(nav_state_progress, "{\"type\":\"nova.nav.progress\",\"step\":2,\"to_maneuver_m\":80,\"remaining_s\":75,"
                              "\"remaining_m\":80,\"leg\":0}");
    nav_banner_t b;
    nav_banner(&s_nav, false, 74460 - 75, &b);
    CHECK_EQ(b.kind, NAV_M_ARRIVE);
    CHECK(!strcmp(b.turn_dist, "80 m"));
    CHECK(!strcmp(b.instruction, "You have arrived at Los Altos Hills"));
    CHECK(!strcmp(b.trip, "1 min 15 s \xC2\xB7 0.1 km (0.0 mi) \xC2\xB7 arrive 20:41"));
    frame(nav_state_progress, "{\"type\":\"nova.nav.progress\",\"step\":99,\"to_maneuver_m\":5}");   /* clamped */
    nav_banner(&s_nav, false, 0, &b);
    CHECK_EQ(b.kind, NAV_M_ARRIVE);
    frame(nav_state_from_frame, k_nav);   /* a new route resets the progress */
    nav_banner(&s_nav, false, 0, &b);
    CHECK_EQ(b.kind, NAV_M_RIGHT);
}

static void legacy_frame(void) {
    memset(&s_nav, 0, sizeof s_nav);
    frame(nav_state_from_frame, "{\"type\":\"nova.nav\",\"active\":true,\"destination\":{\"name\":\"Starbucks\"},"
                                "\"eta_min\":8,\"distance_km\":6.7}");
    nav_banner_t b;
    nav_banner(&s_nav, false, 1000, &b);
    CHECK(!b.turn);   /* no steps from an older pipeline: destination and trip only */
    CHECK(!strcmp(b.trip, "8 min \xC2\xB7 6.7 km (4.2 mi) \xC2\xB7 arrive 00:24"));
    s_nav.eta_min = 7.5;   /* the 1 Hz vehicle deltas keep the trip line moving */
    nav_banner(&s_nav, false, 1000, &b);
    CHECK(!strcmp(b.trip, "7 min 30 s \xC2\xB7 6.7 km (4.2 mi) \xC2\xB7 arrive 00:24"));
    frame(nav_state_from_frame, "{\"type\":\"nova.nav\",\"active\":false}");
    CHECK(!s_nav.active);
}

int main(void) {
    RUN(maneuvers); RUN(distances); RUN(durations); RUN(clock_text); RUN(banner_from_steps); RUN(progress_moves_on);
    RUN(legacy_frame);
    DONE();
}
