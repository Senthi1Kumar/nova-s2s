#include "map/map_engine.h"
#include "map/mapbox_url.h"
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <maplibre_native_c.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif

#define PUMP_BUDGET_MS 4

struct cb_map {
    EGLDisplay dpy;
    EGLConfig cfg;
    EGLContext ctx;
    EGLSurface pbuf;
    mln_runtime rt;
    mln_map map;
    mln_render_session sess;
    char token[256];   /* read by the URL transform on MapLibre worker threads */
    uint32_t w, h, stride;
    uint8_t *px;
    size_t px_cap;
    bool have_frame, dirty, style_ready;
    cb_map_stats_t stats;
};

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static char last_error[256];

static void set_error(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(last_error, sizeof last_error, fmt, ap);
    va_end(ap);
}

const char *cb_map_last_error(void) { return last_error; }

static uint32_t on_log(void *ud, uint32_t severity, uint32_t event, int64_t code, const char *message)
{
    (void)ud;
    (void)event;
    (void)code;
    if(severity >= MLN_LOG_SEVERITY_WARNING) fprintf(stderr, "map: %s\n", message ? message : "?");
    return 1;
}

/* ---- EGL: our own off-screen context, so the map renders the same under the sw and gpu builds ---- */

typedef struct {
    EGLDisplay d;
    EGLContext c;
    EGLSurface draw, read;
    EGLenum api;
} egl_current_t;

static egl_current_t egl_save(void)
{
    egl_current_t s = { eglGetCurrentDisplay(), eglGetCurrentContext(), eglGetCurrentSurface(EGL_DRAW),
                        eglGetCurrentSurface(EGL_READ), eglQueryAPI() };
    return s;
}

static void egl_restore(const cb_map_t *m, const egl_current_t *s)
{
    eglBindAPI(s->api);
    if(s->d != EGL_NO_DISPLAY) eglMakeCurrent(s->d, s->draw, s->read, s->c);
    else if(m->dpy != EGL_NO_DISPLAY) eglMakeCurrent(m->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
}

static EGLDisplay egl_open(void)
{
    const char *want = getenv("CB_MAP_EGL");   /* "surfaceless" (default) or "default" */
    if(!want || strcmp(want, "default") != 0) {
        PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display =
            (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
        if(get_platform_display) {
            EGLDisplay d = get_platform_display(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
            if(d != EGL_NO_DISPLAY && eglInitialize(d, NULL, NULL)) return d;
        }
    }
    EGLDisplay d = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if(d != EGL_NO_DISPLAY && eglInitialize(d, NULL, NULL)) return d;
    return EGL_NO_DISPLAY;
}

static bool egl_setup(cb_map_t *m)
{
    m->dpy = egl_open();
    if(m->dpy == EGL_NO_DISPLAY) {
        set_error("no EGL display (eglInitialize failed)");
        return false;
    }
    const EGLint with_depth[] = { EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                                  EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
                                  EGL_DEPTH_SIZE, 24, EGL_STENCIL_SIZE, 8, EGL_NONE };
    const EGLint plain[] = { EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                             EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE };
    EGLint n = 0;
    if(!eglChooseConfig(m->dpy, with_depth, &m->cfg, 1, &n) || n < 1) {
        if(!eglChooseConfig(m->dpy, plain, &m->cfg, 1, &n) || n < 1) {
            set_error("no EGL config with pbuffer + OpenGL ES 3");
            return false;
        }
    }
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint ctx_attr[] = { EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 0, EGL_NONE };
    m->ctx = eglCreateContext(m->dpy, m->cfg, EGL_NO_CONTEXT, ctx_attr);
    if(m->ctx == EGL_NO_CONTEXT) {
        set_error("eglCreateContext (GLES 3.0) failed: 0x%x", eglGetError());
        return false;
    }
    const EGLint pb_attr[] = { EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE };
    m->pbuf = eglCreatePbufferSurface(m->dpy, m->cfg, pb_attr);
    if(m->pbuf == EGL_NO_SURFACE) {
        set_error("eglCreatePbufferSurface failed: 0x%x", eglGetError());
        return false;
    }
    return true;
}

/* ---- MapLibre ---- */

/* Mapbox URLs (mapbox:// and token-less *.mapbox.com) become authenticated https URLs that MapLibre's own HTTP
   stack fetches and caches. Runs on MapLibre worker threads; the token is immutable while the runtime lives. */
static mln_status mapbox_transform(void *token, uint32_t kind, const char *url, mln_resource_transform_response *out)
{
    (void)kind;
    char buf[2048];
    if(cb_mapbox_url(url, token, buf, sizeof buf) != 1) return MLN_STATUS_OK;
    return mln_resource_transform_response_set_url(out, buf, strlen(buf));
}

static bool ok(mln_status st, const char *what)
{
    if(st == MLN_STATUS_OK) return true;
    set_error("%s failed (status %d)", what, (int)st);
    return false;
}

static bool attach_session(cb_map_t *m)
{
    egl_current_t saved = egl_save();
    eglBindAPI(EGL_OPENGL_ES_API);
    if(!eglMakeCurrent(m->dpy, m->pbuf, m->pbuf, m->ctx)) {
        set_error("eglMakeCurrent failed: 0x%x", eglGetError());
        egl_restore(m, &saved);
        return false;
    }
    typedef const unsigned char *(*get_string_fn)(unsigned int);
    get_string_fn get_string = (get_string_fn)eglGetProcAddress("glGetString");
    if(get_string) {
        const unsigned char *r = get_string(0x1F01 /* GL_RENDERER */), *v = get_string(0x1F02 /* GL_VERSION */);
        fprintf(stderr, "map: GL %s | %s\n", r ? (const char *)r : "?", v ? (const char *)v : "?");
    }
    mln_opengl_owned_texture_descriptor d = mln_opengl_owned_texture_descriptor_default();
    d.extent.size = sizeof d.extent;
    d.extent.width = m->w;
    d.extent.height = m->h;
    d.extent.scale_factor = 1.0;
    d.context.size = sizeof d.context;
    d.context.platform = MLN_OPENGL_CONTEXT_PLATFORM_EGL;
    d.context.ownership = MLN_OPENGL_CONTEXT_OWNERSHIP_SHARED;
    d.context.data.egl.size = sizeof d.context.data.egl;
    d.context.data.egl.display = m->dpy;
    d.context.data.egl.config = m->cfg;
    d.context.data.egl.share_context = m->ctx;
    d.context.data.egl.client_api = MLN_OPENGL_CLIENT_API_GLES;
    d.context.data.egl.get_proc_address = (void *)eglGetProcAddress;
    bool attached = ok(mln_opengl_owned_texture_attach(m->map, &d, &m->sess), "texture session attach");
    egl_restore(m, &saved);
    return attached;
}

cb_map_t *cb_map_create(const cb_map_cfg_t *cfg)
{
    static bool log_installed;
    if(!log_installed) {
        mln_log_set_callback(on_log, NULL);
        log_installed = true;
    }
    if(!cfg || cfg->width == 0 || cfg->height == 0 || !cfg->style_url) {
        set_error("bad map config");
        return NULL;
    }
    if(strncmp(cfg->style_url, "mapbox://", 9) == 0 && !(cfg->access_token && *cfg->access_token)) {
        set_error("a mapbox:// style needs an access token (MAPBOX_API_KEY)");
        return NULL;
    }
    cb_map_t *m = calloc(1, sizeof *m);
    if(!m) return NULL;
    m->dpy = EGL_NO_DISPLAY;
    m->ctx = EGL_NO_CONTEXT;
    m->pbuf = EGL_NO_SURFACE;
    m->w = cfg->width;
    m->h = cfg->height;
    if(!egl_setup(m)) goto fail;

    mln_runtime_options ro = mln_runtime_options_default();
    ro.cache_path = cfg->cache_path && *cfg->cache_path ? cfg->cache_path : ":memory:";
    if(!ok(mln_runtime_create(&ro, &m->rt), "runtime create")) goto fail;

    if(cfg->access_token && *cfg->access_token) {
        snprintf(m->token, sizeof m->token, "%s", cfg->access_token);
        mln_resource_transform t = { sizeof t, mapbox_transform, m->token };
        if(!ok(mln_runtime_set_resource_transform(m->rt, &t), "resource transform")) goto fail;
    }

    mln_map_options mo = mln_map_options_default();
    mo.width = m->w;
    mo.height = m->h;
    mo.scale_factor = 1.0;
    mo.map_mode = MLN_MAP_MODE_CONTINUOUS;
    if(!ok(mln_map_create(m->rt, &mo, &m->map), "map create")) goto fail;
    if(!ok(mln_map_set_event_mask(m->map, MLN_RUNTIME_EVENT_MASK_MAP_RENDER_UPDATE_AVAILABLE |
                                              MLN_RUNTIME_EVENT_MASK_MAP_RENDER_FRAME_FINISHED |
                                              MLN_RUNTIME_EVENT_MASK_MAP_STYLE_LOADED |
                                              MLN_RUNTIME_EVENT_MASK_MAP_LOADING_FAILED |
                                              MLN_RUNTIME_EVENT_MASK_MAP_RENDER_ERROR),
           "event mask"))
        goto fail;
    if(!ok(mln_map_set_style_url(m->map, cfg->style_url), "style url")) goto fail;

    mln_camera_options cam = mln_camera_options_default();
    cam.fields = MLN_CAMERA_OPTION_CENTER | MLN_CAMERA_OPTION_ZOOM | MLN_CAMERA_OPTION_BEARING | MLN_CAMERA_OPTION_PITCH;
    cam.latitude = cfg->lat;
    cam.longitude = cfg->lon;
    cam.zoom = cfg->zoom;
    cam.bearing = cfg->bearing;
    cam.pitch = cfg->pitch;
    if(!ok(mln_map_jump_to(m->map, &cam), "initial camera")) goto fail;

    if(!attach_session(m)) goto fail;
    return m;

fail:
    cb_map_destroy(m);
    return NULL;
}

void cb_map_destroy(cb_map_t *m)
{
    if(!m) return;
    /* children first: a map with a session cannot be destroyed, nor a runtime with a map */
    if(m->sess != MLN_HANDLE_NULL) mln_render_session_destroy(m->sess);
    if(m->map != MLN_HANDLE_NULL) mln_map_destroy(m->map);
    if(m->rt != MLN_HANDLE_NULL) {
        mln_runtime_clear_resource_transform(m->rt);
        mln_runtime_destroy(m->rt);
    }
    if(m->dpy != EGL_NO_DISPLAY) {
        if(eglGetCurrentContext() == m->ctx) eglMakeCurrent(m->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        if(m->pbuf != EGL_NO_SURFACE) eglDestroySurface(m->dpy, m->pbuf);
        if(m->ctx != EGL_NO_CONTEXT) eglDestroyContext(m->dpy, m->ctx);
    }
    free(m->px);
    free(m);
}

static void drain(cb_map_t *m)
{
    mln_runtime_event_batch b = mln_runtime_event_batch_default();
    if(mln_runtime_drain_events(m->rt, 0, &b) != MLN_STATUS_OK) return;
    for(size_t i = 0; i < b.event_count; i++) {
        const mln_runtime_event *e = (const mln_runtime_event *)((const char *)b.events + i * b.event_size);
        if(e->source_type != MLN_RUNTIME_EVENT_SOURCE_MAP || e->source != m->map) continue;
        const char *msg = e->message_size ? b.messages + e->message_offset : "";
        switch(e->type) {
            case MLN_RUNTIME_EVENT_MAP_RENDER_UPDATE_AVAILABLE: m->dirty = true; break;
            case MLN_RUNTIME_EVENT_MAP_RENDER_FRAME_FINISHED:
                if(e->payload.render_frame.needs_repaint) m->dirty = true;
                break;
            case MLN_RUNTIME_EVENT_MAP_STYLE_LOADED: m->style_ready = true; break;
            case MLN_RUNTIME_EVENT_MAP_LOADING_FAILED:
                set_error("map loading failed: %s", msg);
                fprintf(stderr, "map: loading failed: %s\n", msg);
                break;
            case MLN_RUNTIME_EVENT_MAP_RENDER_ERROR:
                set_error("render error: %s", msg);
                fprintf(stderr, "map: render error: %s\n", msg);
                break;
            default: break;
        }
    }
}

static bool read_back(cb_map_t *m)
{
    mln_texture_image_info info = mln_texture_image_info_default();
    if(mln_texture_read_premultiplied_rgba8(m->sess, NULL, 0, &info) != MLN_STATUS_OK) return false;
    if(info.byte_length > m->px_cap) {
        uint8_t *p = realloc(m->px, info.byte_length);
        if(!p) return false;
        m->px = p;
        m->px_cap = info.byte_length;
    }
    double t0 = now_ms();
    if(mln_texture_read_premultiplied_rgba8(m->sess, m->px, m->px_cap, &info) != MLN_STATUS_OK) return false;
    double t1 = now_ms();
    m->w = info.width;
    m->h = info.height;
    m->stride = info.stride;
    /* RGBA bytes -> LVGL's B,G,R,A: swap the R and B bytes of every little-endian word */
    for(uint32_t y = 0; y < info.height; y++) {
        uint32_t *row = (uint32_t *)(m->px + (size_t)y * info.stride);
        for(uint32_t x = 0; x < info.width; x++) {
            uint32_t v = row[x];
            row[x] = (v & 0xff00ff00u) | ((v & 0xffu) << 16) | ((v >> 16) & 0xffu);
        }
    }
    m->have_frame = true;
    m->stats.frames++;
    m->stats.read_ms += t1 - t0;
    m->stats.convert_ms += now_ms() - t1;
    return true;
}

bool cb_map_step(cb_map_t *m)
{
    if(!m) return false;
    double t0 = now_ms();
    mln_runtime_pump(m->rt, 0, PUMP_BUDGET_MS);
    drain(m);
    double t1 = now_ms();
    m->stats.pump_ms += t1 - t0;
    if(!m->dirty) return false;
    mln_render_result res = MLN_RENDER_RESULT_NO_UPDATE;
    bool again = false;
    mln_status st = mln_render_session_render_update(m->sess, &res, &again);
    m->stats.render_ms += now_ms() - t1;
    if(st != MLN_STATUS_OK) return false;
    if(res == MLN_RENDER_RESULT_TARGET_NOT_READY) return false;   /* stay dirty, try next tick */
    m->dirty = again;
    if(res != MLN_RENDER_RESULT_RENDERED) return false;
    return read_back(m);
}

void cb_map_pump(cb_map_t *m)
{
    if(!m) return;
    mln_runtime_pump(m->rt, 0, PUMP_BUDGET_MS);
    drain(m);
}

const uint8_t *cb_map_pixels(const cb_map_t *m, uint32_t *w, uint32_t *h, uint32_t *stride)
{
    if(!m || !m->have_frame) return NULL;
    if(w) *w = m->w;
    if(h) *h = m->h;
    if(stride) *stride = m->stride;
    return m->px;
}

bool cb_map_style_ready(const cb_map_t *m) { return m && m->style_ready; }

bool cb_map_set_style(cb_map_t *m, const char *style_url)
{
    if(!m || !style_url) return false;
    m->style_ready = false;
    m->dirty = true;
    return ok(mln_map_set_style_url(m->map, style_url), "style url");
}

void cb_map_stats(const cb_map_t *m, cb_map_stats_t *out)
{
    if(m && out) *out = m->stats;
}

bool cb_map_resize(cb_map_t *m, uint32_t width, uint32_t height)
{
    if(!m || width == 0 || height == 0) return false;
    return ok(mln_render_session_resize(m->sess, width, height, 1.0), "resize");
}

/* ---- camera ---- */

void cb_map_move_by(cb_map_t *m, double dx, double dy)
{
    if(m) mln_map_move_by(m->map, dx, dy);
}

void cb_map_glide(cb_map_t *m, double dx, double dy, double duration_ms)
{
    if(!m) return;
    mln_animation_options a = mln_animation_options_default();
    a.fields = MLN_ANIMATION_OPTION_DURATION | MLN_ANIMATION_OPTION_EASING;
    a.duration_ms = duration_ms;
    a.easing = (mln_unit_bezier){ 0, 0, 0.3, 1 };   /* Mapbox GL JS inertia easing */
    mln_map_move_by_animated(m->map, dx, dy, &a);
}

void cb_map_scale_by(cb_map_t *m, double scale, double ax, double ay, double duration_ms)
{
    if(!m) return;
    mln_screen_point anchor = { ax, ay };
    if(duration_ms <= 0) {
        mln_map_scale_by(m->map, scale, &anchor);
        return;
    }
    mln_animation_options a = mln_animation_options_default();
    a.fields = MLN_ANIMATION_OPTION_DURATION;
    a.duration_ms = duration_ms;
    mln_map_scale_by_animated(m->map, scale, &anchor, &a);
}

void cb_map_gesture(cb_map_t *m, bool in_progress)
{
    if(!m) return;
    if(in_progress) mln_map_cancel_transitions(m->map);
    mln_map_set_gesture_in_progress(m->map, in_progress);
}

void cb_map_jump_to(cb_map_t *m, double lat, double lon, double zoom, double bearing)
{
    if(!m) return;
    mln_camera_options c = mln_camera_options_default();
    c.fields = MLN_CAMERA_OPTION_CENTER;
    c.latitude = lat;
    c.longitude = lon;
    if(zoom >= 0) {
        c.fields |= MLN_CAMERA_OPTION_ZOOM;
        c.zoom = zoom;
    }
    if(!isnan(bearing)) {
        c.fields |= MLN_CAMERA_OPTION_BEARING;
        c.bearing = bearing;
    }
    mln_map_jump_to(m->map, &c);
}

void cb_map_ease_to(cb_map_t *m, double lat, double lon, double bearing, double duration_ms)
{
    if(!m) return;
    mln_camera_options c = mln_camera_options_default();
    c.fields = MLN_CAMERA_OPTION_CENTER;
    c.latitude = lat;
    c.longitude = lon;
    if(!isnan(bearing)) {
        c.fields |= MLN_CAMERA_OPTION_BEARING;
        c.bearing = bearing;
    }
    mln_animation_options a = mln_animation_options_default();
    a.fields = MLN_ANIMATION_OPTION_DURATION;
    a.duration_ms = duration_ms;
    mln_map_ease_to(m->map, &c, &a);
}

void cb_map_ease_camera(cb_map_t *m, double lat, double lon, double zoom, double bearing, double pitch,
                        double pad_top_px, double duration_ms)
{
    if(!m) return;
    mln_camera_options c = mln_camera_options_default();
    if(!isnan(lat) && !isnan(lon)) {
        c.fields |= MLN_CAMERA_OPTION_CENTER;
        c.latitude = lat;
        c.longitude = lon;
    }
    if(!isnan(zoom)) {
        c.fields |= MLN_CAMERA_OPTION_ZOOM;
        c.zoom = zoom;
    }
    if(!isnan(bearing)) {
        c.fields |= MLN_CAMERA_OPTION_BEARING;
        c.bearing = bearing;
    }
    if(!isnan(pitch)) {
        c.fields |= MLN_CAMERA_OPTION_PITCH;
        c.pitch = pitch;
    }
    if(pad_top_px >= 0) {
        c.fields |= MLN_CAMERA_OPTION_PADDING;
        c.padding = (mln_edge_insets){ pad_top_px, 0, 0, 0 };
    }
    mln_animation_options a = mln_animation_options_default();
    a.fields = MLN_ANIMATION_OPTION_DURATION;
    a.duration_ms = duration_ms;
    mln_map_ease_to(m->map, &c, &a);
}

void cb_map_fit(cb_map_t *m, double south, double west, double north, double east, double pad_px, double duration_ms)
{
    if(!m) return;
    mln_camera_fit_options f = mln_camera_fit_options_default();
    f.fields = MLN_CAMERA_FIT_OPTION_PADDING | MLN_CAMERA_FIT_OPTION_BEARING | MLN_CAMERA_FIT_OPTION_PITCH;
    f.padding = (mln_edge_insets){ pad_px, pad_px, pad_px, pad_px };
    f.bearing = 0;
    f.pitch = 0;
    mln_camera_options c = mln_camera_options_default();
    mln_lat_lng_bounds b = { { south, west }, { north, east } };
    if(!ok(mln_map_camera_for_lat_lng_bounds(m->map, b, &f, &c), "fit")) return;
    c.fields |= MLN_CAMERA_OPTION_PADDING;   /* the overview owns the view: drop the navigation look-ahead */
    c.padding = (mln_edge_insets){ 0, 0, 0, 0 };
    mln_animation_options a = mln_animation_options_default();
    a.fields = MLN_ANIMATION_OPTION_DURATION;
    a.duration_ms = duration_ms;
    mln_map_ease_to(m->map, &c, &a);
}

bool cb_map_camera(cb_map_t *m, double *lat, double *lon, double *zoom, double *bearing)
{
    if(!m) return false;
    mln_camera_options c = mln_camera_options_default();
    if(mln_map_get_camera(m->map, &c) != MLN_STATUS_OK) return false;
    if(lat) *lat = c.latitude;
    if(lon) *lon = c.longitude;
    if(zoom) *zoom = c.zoom;
    if(bearing) *bearing = (c.fields & MLN_CAMERA_OPTION_BEARING) ? c.bearing : 0;
    return true;
}

bool cb_map_pixel_for(cb_map_t *m, double lat, double lon, double *x, double *y)
{
    if(!m) return false;
    mln_screen_point p;
    if(mln_map_pixel_for_lat_lng(m->map, (mln_lat_lng){ lat, lon }, &p) != MLN_STATUS_OK) return false;
    *x = p.x;
    *y = p.y;
    return true;
}

/* ---- overlays ---- */

static mln_buffer_view view(const char *s) { return (mln_buffer_view){ s, s ? strlen(s) : 0 }; }

bool cb_map_set_geojson(cb_map_t *m, const char *source_id, const char *geojson)
{
    if(!m || !m->style_ready || !source_id || !geojson) return false;
    mln_geojson_source_options opts = mln_geojson_source_options_default();
    mln_geojson_source_data data = MLN_HANDLE_NULL;
    if(!ok(mln_geojson_source_data_create(view(geojson), &opts, &data), "geojson parse")) return false;
    mln_status st = mln_map_set_geojson_source_data(m->map, view(source_id), data);
    if(st != MLN_STATUS_OK) st = mln_map_add_geojson_source_data(m->map, view(source_id), data);
    mln_geojson_source_data_destroy(data);
    return ok(st, "geojson source");
}

bool cb_map_set_image(cb_map_t *m, const char *id, const uint8_t *rgba, uint32_t w, uint32_t h)
{
    if(!m || !m->style_ready || !id || !rgba) return false;
    mln_premultiplied_rgba8_image img = mln_premultiplied_rgba8_image_default();
    img.width = w;
    img.height = h;
    img.stride = w * 4;
    img.pixels = rgba;
    img.byte_length = (size_t)w * h * 4;
    mln_style_image_options o = mln_style_image_options_default();
    o.fields = MLN_STYLE_IMAGE_OPTION_PIXEL_RATIO;
    o.pixel_ratio = 1.0f;
    return ok(mln_map_set_style_image(m->map, view(id), &img, &o), "style image");
}

bool cb_map_add_layer_json(cb_map_t *m, const char *layer_json)
{
    if(!m || !m->style_ready || !layer_json) return false;
    return ok(mln_map_add_style_layer_json(m->map, view(layer_json), view("")), "add layer");
}
