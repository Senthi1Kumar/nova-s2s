#pragma once
#include <stddef.h>

/* GeoJSON for the map's own overlays, built on the UI thread when something changes. Each returns the bytes
   written, or 0 when it does not fit in cap (the caller keeps the previous overlay). */

/* The route as runs of equal traffic: one LineString per run with property "c" (u l m h s, from Directions'
   congestion, one letter per segment); without congestion, or past its end, a segment is "u". */
size_t route_geojson_traffic(char *out, size_t cap, const double (*lonlat)[2], int n, const char *congestion);

typedef struct {
    const char *name;
    double lat, lon;
} route_stop_t;

/* Stops numbered from 1 in order (property "n"), then the destination ("dest": true, "n": 0). */
size_t route_geojson_stops(char *out, size_t cap, const route_stop_t *stops, int n, const route_stop_t *dest);

/* The car as a Feature whose "h" (heading, degrees) turns the puck. */
size_t route_geojson_car(char *out, size_t cap, double lat, double lon, double heading);
