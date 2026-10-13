#include "cb_test.h"
#include "live/json.h"
#include "map/route_geojson.h"
#include <string.h>

static char s_out[4096];

static int count(const cbj_t *arr)
{
    int n = 0;
    for(const cbj_t *c = arr ? arr->child : NULL; c; c = c->next) n++;
    return n;
}

static const cbj_t *feature(const cbj_t *fc, int i)
{
    const cbj_t *f = cbj_get(fc, "features") ? cbj_get(fc, "features")->child : NULL;
    while(f && i--) f = f->next;
    return f;
}

static const double k_line[4][2] = { { -122.10, 37.40 }, { -122.11, 37.41 }, { -122.12, 37.42 }, { -122.13, 37.43 } };

static void traffic_runs(void) {
    size_t n = route_geojson_traffic(s_out, sizeof s_out, k_line, 4, "llh");
    CHECK(n > 0);
    cbj_t *j = cbj_parse(s_out, n);
    CHECK(j != NULL);
    CHECK_EQ(count(cbj_get(j, "features")), 2);   /* l, l then h: two runs */
    const cbj_t *a = feature(j, 0), *b = feature(j, 1);
    CHECK(!strcmp(cbj_str(cbj_get(cbj_get(a, "properties"), "c"), ""), "l"));
    CHECK_EQ(count(cbj_get(cbj_get(a, "geometry"), "coordinates")), 3);
    CHECK(!strcmp(cbj_str(cbj_get(cbj_get(b, "properties"), "c"), ""), "h"));
    CHECK_EQ(count(cbj_get(cbj_get(b, "geometry"), "coordinates")), 2);   /* runs share their joint */
    cbj_free(j);
}

static void no_or_bad_congestion(void) {
    size_t n = route_geojson_traffic(s_out, sizeof s_out, k_line, 4, NULL);
    cbj_t *j = cbj_parse(s_out, n);
    CHECK_EQ(count(cbj_get(j, "features")), 1);
    CHECK(!strcmp(cbj_str(cbj_get(cbj_get(feature(j, 0), "properties"), "c"), ""), "u"));
    CHECK_EQ(count(cbj_get(cbj_get(feature(j, 0), "geometry"), "coordinates")), 4);
    cbj_free(j);
    n = route_geojson_traffic(s_out, sizeof s_out, k_line, 4, "x");   /* unknown letter, then too short */
    j = cbj_parse(s_out, n);
    CHECK_EQ(count(cbj_get(j, "features")), 1);
    cbj_free(j);
    CHECK_EQ(route_geojson_traffic(s_out, 40, k_line, 4, "llh"), 0);   /* does not fit */
    CHECK_EQ(route_geojson_traffic(s_out, sizeof s_out, k_line, 1, NULL), 0);
}

static void stops_and_dest(void) {
    route_stop_t stops[2] = { { "Village \"Mail\" Center", 37.40, -122.11 }, { "Bullis Charter School", 37.37, -122.12 } };
    route_stop_t dest = { "Los Altos Hills", 37.36, -122.13 };
    size_t n = route_geojson_stops(s_out, sizeof s_out, stops, 2, &dest);
    cbj_t *j = cbj_parse(s_out, n);
    CHECK(j != NULL);
    CHECK_EQ(count(cbj_get(j, "features")), 3);
    const cbj_t *p0 = cbj_get(feature(j, 0), "properties"), *p2 = cbj_get(feature(j, 2), "properties");
    CHECK(!strcmp(cbj_str(cbj_get(p0, "name"), ""), "Village \"Mail\" Center"));   /* escaped and back */
    CHECK_EQ((int)cbj_num(cbj_get(p0, "n"), 0), 1);
    CHECK(!cbj_bool(cbj_get(p0, "dest"), true));
    CHECK(cbj_bool(cbj_get(p2, "dest"), false));
    cbj_free(j);
}

static void car(void) {
    size_t n = route_geojson_car(s_out, sizeof s_out, 37.4419, -122.143, 123.0);
    cbj_t *j = cbj_parse(s_out, n);
    CHECK(j != NULL);
    CHECK(cbj_num(cbj_get(cbj_get(j, "properties"), "h"), 0) == 123.0);
    const cbj_t *c = cbj_get(cbj_get(j, "geometry"), "coordinates");
    CHECK(c && c->child && cbj_num(c->child, 0) == -122.143);
    cbj_free(j);
}

int main(void) {
    RUN(traffic_runs); RUN(no_or_bad_congestion); RUN(stops_and_dest); RUN(car);
    DONE();
}
