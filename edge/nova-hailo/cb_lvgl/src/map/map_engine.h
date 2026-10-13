#pragma once
#include <stdbool.h>
#include <stdint.h>

/* A MapLibre Native map rendered off-screen (own EGL context, OpenGL ES 3, session-owned texture) and read
   back into a CPU buffer in LVGL's ARGB8888 byte order (B, G, R, A). Everything runs on the calling thread:
   cb_map_step() pumps the runtime with a small budget and renders only when the map has something new, so it
   fits inside an LVGL timer. Mapbox styles (mapbox://…) need an access token. */
typedef struct cb_map cb_map_t;

typedef struct {
    uint32_t width, height;     /* logical = physical pixels */
    double lat, lon, zoom, bearing, pitch;
    const char *style_url;      /* mapbox://styles/…, https://…, or file://… */
    const char *access_token;   /* Mapbox token, or NULL */
    const char *cache_path;     /* NULL = in memory */
} cb_map_cfg_t;

cb_map_t *cb_map_create(const cb_map_cfg_t *cfg);   /* NULL on failure; reason in cb_map_last_error() */
const char *cb_map_last_error(void);
void cb_map_destroy(cb_map_t *m);

bool cb_map_step(cb_map_t *m);   /* true when a new frame is in the buffer */
void cb_map_pump(cb_map_t *m);   /* runtime work only (network, parsing), no render: for a hidden map */
const uint8_t *cb_map_pixels(const cb_map_t *m, uint32_t *w, uint32_t *h, uint32_t *stride);
bool cb_map_style_ready(const cb_map_t *m);   /* sources and layers can be added */
bool cb_map_set_style(cb_map_t *m, const char *style_url);   /* our sources and layers must be added again */
bool cb_map_resize(cb_map_t *m, uint32_t width, uint32_t height);

void cb_map_move_by(cb_map_t *m, double dx, double dy);
void cb_map_glide(cb_map_t *m, double dx, double dy, double duration_ms);   /* inertia pan, ease-out */
void cb_map_scale_by(cb_map_t *m, double scale, double anchor_x, double anchor_y, double duration_ms);
void cb_map_gesture(cb_map_t *m, bool in_progress);
void cb_map_jump_to(cb_map_t *m, double lat, double lon, double zoom /* < 0 keeps */, double bearing /* NAN keeps */);
void cb_map_ease_to(cb_map_t *m, double lat, double lon, double bearing /* NAN keeps */, double duration_ms);
bool cb_map_camera(cb_map_t *m, double *lat, double *lon, double *zoom, double *bearing);
/* Centre, zoom, bearing and pitch in one eased move; NAN keeps a field, pad_top_px < 0 keeps the padding.
   Navigation looks ahead by padding the top, which puts the car in the lower part of the view. */
void cb_map_ease_camera(cb_map_t *m, double lat, double lon, double zoom, double bearing, double pitch,
                        double pad_top_px, double duration_ms);
/* Eases to the camera that shows the box with pad_px to spare, north up and flat. */
void cb_map_fit(cb_map_t *m, double south, double west, double north, double east, double pad_px, double duration_ms);
/* A runtime style image from premultiplied RGBA8 (stride w * 4), e.g. the car puck; a restyle drops it. */
bool cb_map_set_image(cb_map_t *m, const char *id, const uint8_t *rgba, uint32_t w, uint32_t h);
bool cb_map_pixel_for(cb_map_t *m, double lat, double lon, double *x, double *y);

typedef struct {
    uint32_t frames;
    double pump_ms, render_ms, read_ms, convert_ms;   /* wall time on the calling thread, totals since create */
} cb_map_stats_t;
void cb_map_stats(const cb_map_t *m, cb_map_stats_t *out);

bool cb_map_set_geojson(cb_map_t *m, const char *source_id, const char *geojson);   /* adds or replaces */
bool cb_map_add_layer_json(cb_map_t *m, const char *layer_json);
