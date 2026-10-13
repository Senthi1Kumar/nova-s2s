#include "map/map_view.h"
#include "cb_theme.h"
#include "map/map_gesture.h"
#include "src/misc/cache/lv_cache.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if CB_HAVE_MAP
#include "map/map_worker.h"
#endif

#define FAST_MS 33     /* render pace while touched, gliding or zooming */
#define FOLLOW_MS 66   /* following a moving car */
#define IDLE_MS 100    /* nothing moving: tiles and fades still land */
#define UI_MS 16       /* the UI picks up finished frames this often */

typedef struct {
    lv_obj_t *obj, *placeholder;
    lv_timer_t *timer;
    map_view_cfg_t cfg;
    char style[256], token[256];
#if CB_HAVE_MAP
    cb_mapw_t *mw;
#endif
    lv_image_dsc_t img;
    bool have_frame, visible, follow, touching, failed, have_car, night, fixed_style;
    uint32_t motion_until;
    int32_t travel;
    cb_drag_t drag;
    double car_lat, car_lon, car_heading;
    char *route_json;
} mv_t;

static mv_t *get(lv_obj_t *o) { return lv_obj_get_user_data(o); }

#define STYLE_NIGHT "mapbox://styles/mapbox/navigation-night-v1"
#define STYLE_DAY "mapbox://styles/mapbox/streets-v12"
#define STYLE_NIGHT_FREE "https://tiles.openfreemap.org/styles/dark"
#define STYLE_DAY_FREE "https://tiles.openfreemap.org/styles/liberty"

/* Read on the map worker when a style loads: the night style draws no place names, so we add them. */
static _Atomic bool s_add_pois;

static const char *style_for(const mv_t *v, bool night)
{
    if(v->token[0]) return night ? STYLE_NIGHT : STYLE_DAY;
    return night ? STYLE_NIGHT_FREE : STYLE_DAY_FREE;
}

static void motion(mv_t *v, uint32_t ms) { v->motion_until = lv_tick_get() + ms; }

#if CB_HAVE_MAP

static const char *EMPTY = "{\"type\":\"FeatureCollection\",\"features\":[]}";

/* Runs on the map worker once the style is loaded: our sources and layers on top of the basemap. */
static void add_layers(cb_map_t *m)
{
    if(s_add_pois)   /* Mapbox Streets v8 poi_label, text only: the night sprite lacks the maki icons */
        cb_map_add_layer_json(m, "{\"id\":\"cb-poi\",\"type\":\"symbol\",\"source\":\"composite\","
                                 "\"source-layer\":\"poi_label\",\"minzoom\":15,"
                                 "\"filter\":[\"<=\",[\"get\",\"filterrank\"],3],"
                                 "\"layout\":{\"text-field\":[\"coalesce\",[\"get\",\"name_en\"],[\"get\",\"name\"]],"
                                 "\"text-size\":12,\"text-max-width\":8,"
                                 "\"text-font\":[\"DIN Pro Medium\",\"Arial Unicode MS Regular\"]},"
                                 "\"paint\":{\"text-color\":\"#c9d3e3\",\"text-halo-color\":\"#10141c\","
                                 "\"text-halo-width\":1.2}}");
    cb_map_set_geojson(m, "cb-route", EMPTY);
    cb_map_set_geojson(m, "cb-car", EMPTY);
    cb_map_add_layer_json(m, "{\"id\":\"cb-route-casing\",\"type\":\"line\",\"source\":\"cb-route\","
                             "\"layout\":{\"line-join\":\"round\",\"line-cap\":\"round\"},"
                             "\"paint\":{\"line-color\":\"#0b2f3d\",\"line-width\":11}}");
    cb_map_add_layer_json(m, "{\"id\":\"cb-route-line\",\"type\":\"line\",\"source\":\"cb-route\","
                             "\"layout\":{\"line-join\":\"round\",\"line-cap\":\"round\"},"
                             "\"paint\":{\"line-color\":\"#4cd6ff\",\"line-width\":6}}");
    cb_map_add_layer_json(m, "{\"id\":\"cb-car-halo\",\"type\":\"circle\",\"source\":\"cb-car\","
                             "\"paint\":{\"circle-radius\":22,\"circle-color\":\"#4cd6ff\",\"circle-opacity\":0.18}}");
    cb_map_add_layer_json(m, "{\"id\":\"cb-car-dot\",\"type\":\"circle\",\"source\":\"cb-car\","
                             "\"paint\":{\"circle-radius\":9,\"circle-color\":\"#4cd6ff\","
                             "\"circle-stroke-color\":\"#ffffff\",\"circle-stroke-width\":3}}");
}

static void take_frame(mv_t *v)
{
    uint32_t w, h, stride;
    const uint8_t *px = cb_mapw_take(v->mw, &w, &h, &stride);
    if(!px) return;
    if(v->have_frame) lv_image_cache_drop(&v->img);
    v->img.header.magic = LV_IMAGE_HEADER_MAGIC;
    v->img.header.cf = LV_COLOR_FORMAT_XRGB8888;
    v->img.header.w = w;
    v->img.header.h = h;
    v->img.header.stride = stride;
    v->img.data = px;
    v->img.data_size = stride * h;
    v->have_frame = true;
    lv_obj_set_hidden(v->placeholder, true);
    lv_obj_invalidate(v->obj);
}

static bool ensure_map(mv_t *v)
{
    if(v->mw) return true;
    int32_t w = lv_obj_get_width(v->obj), h = lv_obj_get_height(v->obj);
    if(w < 16 || h < 16) return false;
    cb_map_cfg_t c = { .width = (uint32_t)w, .height = (uint32_t)h, .lat = v->have_car ? v->car_lat : v->cfg.lat,
                       .lon = v->have_car ? v->car_lon : v->cfg.lon, .zoom = v->cfg.zoom, .style_url = v->style,
                       .access_token = v->token };
    v->mw = cb_mapw_create(&c, add_layers);
    if(!v->mw) return false;
    if(v->have_car) {   /* whatever arrived before the worker existed */
        char b[192];
        snprintf(b, sizeof b, "{\"type\":\"Point\",\"coordinates\":[%.7f,%.7f]}", v->car_lon, v->car_lat);
        cb_mapw_set_geojson(v->mw, "cb-car", b);
    }
    if(v->route_json) cb_mapw_set_geojson(v->mw, "cb-route", v->route_json);
    return true;
}

static void tick(lv_timer_t *t)
{
    mv_t *v = lv_timer_get_user_data(t);
    if(v->failed || !ensure_map(v)) return;
    char why[160];
    if(cb_mapw_failed(v->mw, why, sizeof why)) {
        v->failed = true;
        lv_label_set_text_fmt(v->placeholder, "Map unavailable: %s", why);
        lv_timer_set_period(t, 1000);
        return;
    }
    bool fast = v->touching || (int32_t)(v->motion_until - lv_tick_get()) > 0;
    cb_mapw_set_period(v->mw, !v->visible ? 0 : fast ? FAST_MS : v->follow && v->have_car ? FOLLOW_MS : IDLE_MS);
    lv_timer_set_period(t, v->visible ? UI_MS : 250);
    if(v->visible) take_frame(v);
}

#endif

static void draw_cb(lv_event_t *e)
{
    mv_t *v = get(lv_event_get_target_obj(e));
    if(!v->have_frame) return;
    lv_area_t a;
    lv_obj_get_coords(v->obj, &a);
    a.x2 = a.x1 + (int32_t)v->img.header.w - 1;
    a.y2 = a.y1 + (int32_t)v->img.header.h - 1;
    lv_draw_image_dsc_t d;
    lv_draw_image_dsc_init(&d);
    d.src = &v->img;
    lv_draw_image(lv_event_get_layer(e), &d, &a);
}

static void size_cb(lv_event_t *e)
{
#if CB_HAVE_MAP
    mv_t *v = get(lv_event_get_target_obj(e));
    if(v->mw) {
        cb_mapw_resize(v->mw, (uint32_t)lv_obj_get_width(v->obj), (uint32_t)lv_obj_get_height(v->obj));
        motion(v, 500);
    }
#else
    LV_UNUSED(e);
#endif
}

static void local_point(mv_t *v, int32_t *x, int32_t *y)
{
    lv_point_t p;
    lv_indev_get_point(lv_indev_active(), &p);
    lv_area_t a;
    lv_obj_get_coords(v->obj, &a);
    *x = p.x - a.x1;
    *y = p.y - a.y1;
}

static void input_cb(lv_event_t *e)
{
    mv_t *v = get(lv_event_get_target_obj(e));
#if CB_HAVE_MAP
    if(!v->mw || v->failed || !lv_indev_active()) return;
    lv_event_code_t code = lv_event_get_code(e);
    uint32_t now = lv_tick_get();
    int32_t x, y;
    local_point(v, &x, &y);
    if(code == LV_EVENT_PRESSED) {
        v->touching = true;
        v->travel = 0;
        cb_drag_begin(&v->drag, x, y, now);
        cb_mapw_gesture(v->mw, true);
        motion(v, 400);
    }
    else if(code == LV_EVENT_PRESSING && v->touching) {
        int32_t dx, dy;
        cb_drag_move(&v->drag, x, y, now, &dx, &dy);
        if(dx || dy) {
            v->travel += abs(dx) + abs(dy);
            if(v->travel > 8) v->follow = false;
            cb_mapw_move_by(v->mw, dx, dy);
        }
    }
    else if(v->touching) {   /* released or lost */
        v->touching = false;
        double fx, fy, ms;
        if(v->travel > 8 && cb_drag_end(&v->drag, now, &fx, &fy, &ms)) {
            cb_mapw_glide(v->mw, fx, fy, ms);
            motion(v, (uint32_t)ms + 200);
        }
        else motion(v, 400);
        cb_mapw_gesture(v->mw, false);
        if(v->travel <= 8 && code == LV_EVENT_RELEASED && cb_tap_is_double(&v->drag, x, y, now)) {
            cb_mapw_scale_by(v->mw, 2.0, x, y, 300);
            motion(v, 900);
        }
    }
#else
    LV_UNUSED(v);
#endif
}

static void delete_cb(lv_event_t *e)
{
    mv_t *v = get(lv_event_get_target_obj(e));
    if(v->timer) lv_timer_delete(v->timer);
#if CB_HAVE_MAP
    cb_mapw_destroy(v->mw);
#endif
    free(v->route_json);
    free(v);
}

lv_obj_t *map_view_create(lv_obj_t *parent, const map_view_cfg_t *cfg)
{
    mv_t *v = calloc(1, sizeof *v);
    v->cfg = *cfg;
    const char *token = cfg->access_token ? cfg->access_token : getenv("MAPBOX_API_KEY");
    snprintf(v->token, sizeof v->token, "%s", token ? token : "");
    const char *style = cfg->style_url ? cfg->style_url : getenv("CB_MAP_STYLE");
    v->night = true;
    v->fixed_style = style && *style;
    if(!v->fixed_style) style = style_for(v, true);
    s_add_pois = !v->fixed_style && v->token[0];
    snprintf(v->style, sizeof v->style, "%s", style);
    v->follow = true;
    v->visible = true;

    v->obj = lv_obj_create(parent);
    lv_obj_remove_style_all(v->obj);
    lv_obj_set_user_data(v->obj, v);
    lv_obj_set_style_bg_color(v->obj, lv_color_hex(0x15181d), 0);
    lv_obj_set_style_bg_opa(v->obj, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(v->obj, false);
    lv_obj_set_clickable(v->obj, true);
    /* the map owns its drags: they never reach the page swiper */
    lv_obj_set_scroll_chain(v->obj, false);
    lv_obj_set_gesture_bubble(v->obj, false);
    lv_obj_add_event_cb(v->obj, draw_cb, LV_EVENT_DRAW_MAIN, NULL);
    lv_obj_add_event_cb(v->obj, size_cb, LV_EVENT_SIZE_CHANGED, NULL);
    lv_obj_add_event_cb(v->obj, input_cb, LV_EVENT_PRESSED, NULL);
    lv_obj_add_event_cb(v->obj, input_cb, LV_EVENT_PRESSING, NULL);
    lv_obj_add_event_cb(v->obj, input_cb, LV_EVENT_RELEASED, NULL);
    lv_obj_add_event_cb(v->obj, input_cb, LV_EVENT_PRESS_LOST, NULL);
    lv_obj_add_event_cb(v->obj, delete_cb, LV_EVENT_DELETE, NULL);

    v->placeholder = lv_label_create(v->obj);
    lv_obj_set_style_text_font(v->placeholder, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_text_color(v->placeholder, cb_col(CB_TEXT_LO), 0);
    lv_obj_center(v->placeholder);
#if CB_HAVE_MAP
    lv_label_set_text(v->placeholder, "Loading map\xE2\x80\xA6");
    v->timer = lv_timer_create(tick, UI_MS, v);
#else
    lv_label_set_text(v->placeholder, "Map not built (MapLibre Native package missing)");
#endif
    return v->obj;
}

void map_view_set_active(lv_obj_t *mv, bool visible)
{
    mv_t *v = get(mv);
    v->visible = visible;
    if(visible) motion(v, 600);
    if(v->timer) lv_timer_ready(v->timer);
}

void map_view_set_car(lv_obj_t *mv, double lat, double lon, double heading)
{
    mv_t *v = get(mv);
    v->car_lat = lat;
    v->car_lon = lon;
    v->car_heading = heading;
    v->have_car = true;
#if CB_HAVE_MAP
    if(!v->mw) return;
    char b[192];
    snprintf(b, sizeof b, "{\"type\":\"Point\",\"coordinates\":[%.7f,%.7f]}", lon, lat);
    cb_mapw_set_geojson(v->mw, "cb-car", b);
    if(v->follow && !v->touching) cb_mapw_ease_to(v->mw, lat, lon, 220);
#endif
}

void map_view_set_route(lv_obj_t *mv, const double (*ll)[2], int n)
{
    mv_t *v = get(mv);
    free(v->route_json);
    v->route_json = NULL;
    if(n >= 2) {
        size_t cap = 96 + (size_t)n * 32;
        char *b = malloc(cap);
        size_t len = (size_t)snprintf(b, cap, "{\"type\":\"LineString\",\"coordinates\":[");
        for(int i = 0; i < n && len < cap; i++)
            len += (size_t)snprintf(b + len, cap - len, "%s[%.6f,%.6f]", i ? "," : "", ll[i][0], ll[i][1]);
        if(len < cap) snprintf(b + len, cap - len, "]}");
        v->route_json = b;
    }
#if CB_HAVE_MAP
    if(v->mw) cb_mapw_set_geojson(v->mw, "cb-route", v->route_json ? v->route_json : EMPTY);
#endif
    motion(v, 300);
}

void map_view_set_follow(lv_obj_t *mv, bool follow)
{
    mv_t *v = get(mv);
    v->follow = follow;
#if CB_HAVE_MAP
    if(follow && v->have_car && v->mw) {
        cb_mapw_ease_to(v->mw, v->car_lat, v->car_lon, 600);
        motion(v, 900);
    }
#endif
}

bool map_view_following(lv_obj_t *mv) { return get(mv)->follow; }

void map_view_zoom(lv_obj_t *mv, double factor)
{
#if CB_HAVE_MAP
    mv_t *v = get(mv);
    if(!v->mw) return;
    cb_mapw_scale_by(v->mw, factor, lv_obj_get_width(v->obj) / 2.0, lv_obj_get_height(v->obj) / 2.0, 280);
    motion(v, 700);
#else
    LV_UNUSED(mv);
    LV_UNUSED(factor);
#endif
}

void map_view_set_night(lv_obj_t *mv, bool night)
{
    mv_t *v = get(mv);
    if(v->night == night) return;
    v->night = night;
    snprintf(v->style, sizeof v->style, "%s", style_for(v, night));
    s_add_pois = night && v->token[0];
    v->fixed_style = false;
#if CB_HAVE_MAP
    if(v->mw) {
        cb_mapw_set_style(v->mw, v->style);
        motion(v, 3000);   /* tiles, glyphs and sprites of the new style arrive over the next seconds */
    }
#endif
}

bool map_view_night(lv_obj_t *mv) { return get(mv)->night; }

void map_view_wheel(lv_obj_t *mv, int32_t x, int32_t y, float notches)
{
    mv_t *v = get(mv);
    lv_area_t a;
    lv_obj_get_coords(v->obj, &a);
    if(!v->visible || notches == 0 || x < a.x1 || x > a.x2 || y < a.y1 || y > a.y2) return;
#if CB_HAVE_MAP
    if(!v->mw || v->failed) return;
    cb_mapw_scale_by(v->mw, pow(2.0, notches * 0.5), x - a.x1, y - a.y1, 140);   /* a notch = half a zoom level */
    motion(v, 500);
#endif
}

bool map_view_live(lv_obj_t *mv)
{
#if CB_HAVE_MAP
    mv_t *v = get(mv);
    return v->mw != NULL && !v->failed;
#else
    LV_UNUSED(mv);
    return false;
#endif
}
