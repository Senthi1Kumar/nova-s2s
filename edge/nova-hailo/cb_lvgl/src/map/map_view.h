#pragma once
#include "lvgl.h"
#include "map/route_geojson.h"
#include <stdbool.h>

/* The live map as an LVGL object: Mapbox vector tiles rendered by MapLibre Native off-screen (map_engine) and
   drawn as an image. Drag pans like Google Maps (with a glide on release), double tap zooms in, and follow mode
   keeps the car centred until the driver drags. Frames are rendered only while something moves: up to 30 fps
   while touched or animating, 15 fps while following a moving car, none while idle or while the page is hidden.
   Without MapLibre (no CB_HAVE_MAP) or without a token for a mapbox:// style it shows a placeholder. */
typedef struct {
    const char *style_url;      /* NULL: CB_MAP_STYLE env, else the night style (Mapbox with a token, else OpenFreeMap) */
    const char *access_token;   /* NULL: MAPBOX_API_KEY env */
    double lat, lon, zoom;
} map_view_cfg_t;

lv_obj_t *map_view_create(lv_obj_t *parent, const map_view_cfg_t *cfg);
void map_view_set_active(lv_obj_t *mv, bool visible);
void map_view_set_car(lv_obj_t *mv, double lat, double lon, double heading_deg);   /* the arrow puck */
/* n < 2 clears. congestion: Directions' one letter per segment (u l m h s) colors the line, NULL = plain. A new
   route while navigating is first shown whole (overview), then the camera returns to the car. */
void map_view_set_route(lv_obj_t *mv, const double (*lonlat)[2], int n, const char *congestion);
void map_view_set_stops(lv_obj_t *mv, const route_stop_t *stops, int n, const route_stop_t *dest);   /* NULL dest clears */
void map_view_set_follow(lv_obj_t *mv, bool follow);
bool map_view_following(lv_obj_t *mv);
/* Navigating: while following, the map turns heading-up with a tilted look-ahead (the car low in the view). */
void map_view_set_nav(lv_obj_t *mv, bool navigating);
void map_view_recenter(lv_obj_t *mv);   /* follow again at street level (heading-up when navigating) */
void map_view_north_up(lv_obj_t *mv);   /* the compass: north up and flat until the next recenter */
void map_view_overview(lv_obj_t *mv);   /* the whole route in view until the next recenter */
double map_view_bearing(lv_obj_t *mv);  /* degrees clockwise from north, as drawn */
void map_view_zoom(lv_obj_t *mv, double factor);   /* about the centre, eased */
bool map_view_live(lv_obj_t *mv);                  /* MapLibre is rendering (not the placeholder) */
/* Night: navigation-night (live traffic) with place names added; day: streets (shops, restaurants, transit). */
void map_view_set_night(lv_obj_t *mv, bool night);
bool map_view_night(lv_obj_t *mv);
void map_view_wheel(lv_obj_t *mv, int32_t x, int32_t y, float notches);   /* screen point; zooms about it */
