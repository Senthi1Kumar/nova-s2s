#include "map/map_worker.h"
#include "map/map_follow.h"
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_SOURCES 4
#define ZOOM_TAU_MS 180.0      /* the follow camera's zoom eases toward its target */
#define BEARING_TAU_MS 350.0   /* heading-up turns ease, so a bend in the road does not snap the map */
#define VIEW_TAU_MS 250.0      /* pitch and look-ahead */
#define REGRIP_MS 300.0        /* a tap paused following: blend back over this */
#define PUCK_MARGIN 40.0       /* the car counts as in view this far past the edge (the puck is drawn whole) */

typedef enum {
    OP_MOVE, OP_GLIDE, OP_SCALE, OP_GESTURE, OP_EASE, OP_CAMERA, OP_FIT, OP_RESIZE, OP_STYLE, OP_FOLLOW,
    OP_FOLLOW_ZOOM
} op_t;

typedef struct cmd {
    op_t op;
    double a, b, c, d, e, f, g;
    char *s;   /* OP_STYLE: owned url */
    struct cmd *next;
} cmd_t;

typedef struct {
    char id[32];
    char *json;   /* pending, owned */
    char *last;   /* last applied, kept for a restyle */
} source_t;

/* The follow camera. Worker thread only. */
typedef struct {
    bool on, up, gesture, blending;
    double zoom, pitch, pad;          /* targets */
    double cz, cb, cp, cpad;          /* the camera now, easing toward them */
    double s_lat, s_lon;              /* the centre when the blend began */
    double blend_t0, blend_ms, last_t;
    double pad_now;                   /* the padding the map has, whoever set it */
    bool have_car;
    double car_lat, car_lon, car_hdg;   /* the car as drawn in the frame being rendered */
} follow_t;

struct cb_mapw {
    pthread_t thread;
    pthread_mutex_t mu;
    pthread_cond_t cv;
    bool stop, failed;
    char why[160];
    cb_map_cfg_t cfg;
    cb_mapw_style_cb_t on_style;
    cmd_t *head, *tail;
    _Atomic uint32_t period_ms;
    source_t src[MAX_SOURCES];
    map_track_t track;   /* under mu */
    /* Frames by pointer: the engine renders into its own buffer and swaps it for `back`; `ready` waits for the
       UI, which shows `front`. The worker never touches `front`. */
    uint8_t *back, *ready, *front;
    size_t back_cap, ready_cap, front_cap;
    uint32_t w, h, stride;
    uint64_t ready_seq, taken_seq;
    cb_map_tag_t tag;
    uint32_t tag_seq;
    uint32_t view_w, view_h;   /* worker thread */
    follow_t fl;               /* worker thread */
    /* CB_MAP_STATS=1: one line every 5 s on stderr. Worker thread. */
    bool stats_on;
    double st_t0, st_n, st_sum, st_sq, st_lat, st_lon, st_t;
    uint32_t st_frames, st_tags;
    cb_map_stats_t st_eng;
};

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static void push(cb_mapw_t *w, cmd_t c)
{
    cmd_t *n = malloc(sizeof *n);
    if(!n) return;
    *n = c;
    n->next = NULL;
    pthread_mutex_lock(&w->mu);
    if(c.op == OP_MOVE && w->tail && w->tail->op == OP_MOVE) {   /* coalesce finger moves */
        w->tail->a += c.a;
        w->tail->b += c.b;
        free(n);
    }
    else {
        if(w->tail) w->tail->next = n;
        else w->head = n;
        w->tail = n;
    }
    pthread_cond_signal(&w->cv);
    pthread_mutex_unlock(&w->mu);
}

/* ---- the follow camera ---- */

/* Start following from wherever the camera is now, so engaging never jumps. */
static void grip(cb_mapw_t *w, cb_map_t *m, double ms)
{
    follow_t *f = &w->fl;
    double lat, lon, zoom, bearing, pitch;
    if(!cb_map_get_view(m, &lat, &lon, &zoom, &bearing, &pitch)) return;
    f->s_lat = lat;
    f->s_lon = lon;
    f->cz = zoom;
    f->cb = bearing;
    f->cp = pitch;
    f->cpad = f->pad_now;
    f->blending = ms > 0;
    f->blend_t0 = now_ms();
    f->blend_ms = ms;
    f->last_t = 0;
}

static void follow_set(cb_mapw_t *w, cb_map_t *m, const cmd_t *c)
{
    follow_t *f = &w->fl;
    if(!isnan(c->c)) f->zoom = c->c;
    if(!isnan(c->d)) f->pitch = c->d;
    if(!isnan(c->e)) f->pad = c->e;
    f->up = c->b != 0;
    f->on = c->a != 0;
    if(f->on) grip(w, m, c->f);
}

/* Moves the camera onto the car as it is drawn now: called before each render while following. */
static void follow_frame(cb_mapw_t *w, cb_map_t *m, double now)
{
    follow_t *f = &w->fl;
    double dt = f->last_t > 0 ? now - f->last_t : 0;
    if(dt > 100) dt = 100;   /* after a stall, ease on rather than jump */
    f->last_t = now;
    double k = f->blending ? f->blend_ms / 3.0 : 0;   /* a blend eases the whole view at its own pace */
    f->cz = map_approach(f->cz, f->zoom, dt, k > 0 ? k : ZOOM_TAU_MS);
    f->cb = map_angle_approach(f->cb, f->up ? f->car_hdg : 0, dt, k > 0 ? k : BEARING_TAU_MS);
    f->cp = map_approach(f->cp, f->pitch, dt, k > 0 ? k : VIEW_TAU_MS);
    f->cpad = map_approach(f->cpad, f->pad, dt, k > 0 ? k : VIEW_TAU_MS);
    double lat = f->car_lat, lon = f->car_lon;
    if(f->blending) {
        double u = map_smoothstep((now - f->blend_t0) / f->blend_ms);
        lat = f->s_lat + (lat - f->s_lat) * u;
        lon = f->s_lon + (lon - f->s_lon) * u;
        if(u >= 1) f->blending = false;
    }
    cb_map_set_view(m, lat, lon, f->cz, f->cb, f->cp, f->cpad);
    f->pad_now = f->cpad;
    if(w->stats_on) {   /* the camera's speed frame to frame: steady when smooth, surging when it steps */
        double dt_s = (now - w->st_t) / 1000.0;
        if((w->st_lat != 0 || w->st_lon != 0) && dt_s > 0.001 && dt_s < 0.2) {
            double v = map_distance_m(w->st_lat, w->st_lon, lat, lon) / dt_s;
            w->st_n++;
            w->st_sum += v;
            w->st_sq += v * v;
        }
        w->st_lat = lat;
        w->st_lon = lon;
        w->st_t = now;
    }
}

static void stats_tick(cb_mapw_t *w, cb_map_t *m, double now)
{
    if(!w->stats_on) return;
    if(w->st_t0 == 0) {
        w->st_t0 = now;
        cb_map_stats(m, &w->st_eng);
        return;
    }
    double span = now - w->st_t0;
    if(span < 5000) return;
    cb_map_stats_t e;
    cb_map_stats(m, &e);
    uint32_t n = e.frames - w->st_eng.frames ? e.frames - w->st_eng.frames : 1;
    fprintf(stderr, "map: %.1f fps, %u puck-only | pump %.1f render %.1f read %.1f convert %.1f ms",
            w->st_frames * 1000.0 / span, w->st_tags, (e.pump_ms - w->st_eng.pump_ms) / n,
            (e.render_ms - w->st_eng.render_ms) / n, (e.read_ms - w->st_eng.read_ms) / n,
            (e.convert_ms - w->st_eng.convert_ms) / n);
    if(w->st_n > 1) {
        double mean = w->st_sum / w->st_n, var = w->st_sq / w->st_n - mean * mean;
        fprintf(stderr, " | camera %.1f m/s, varies %.0f%%", mean, mean > 0 ? 100.0 * sqrt(fmax(var, 0)) / mean : 0);
    }
    double la, lo, z, b, p;
    if(cb_map_get_view(m, &la, &lo, &z, &b, &p))
        fprintf(stderr, " | view z%.1f pitch %.0f bearing %.0f%s", z, p, b, w->fl.on ? " following" : "");
    fputc('\n', stderr);
    w->st_t0 = now;
    w->st_eng = e;
    w->st_frames = w->st_tags = 0;
    w->st_n = w->st_sum = w->st_sq = 0;
}

/* The engine calls this right after each render: the car's screen point under the camera that drew it. */
static void tag_car(cb_map_t *m, void *user, cb_map_tag_t *out)
{
    cb_mapw_t *w = user;
    const follow_t *f = &w->fl;
    double lat, lon, zoom, bearing = 0, pitch;
    if(cb_map_get_view(m, &lat, &lon, &zoom, &bearing, &pitch)) out->bearing = bearing;
    double x, y;
    if(!f->have_car || !cb_map_pixel_for(m, f->car_lat, f->car_lon, &x, &y)) return;
    out->car_on = x > -PUCK_MARGIN && y > -PUCK_MARGIN && x < w->view_w + PUCK_MARGIN && y < w->view_h + PUCK_MARGIN;
    out->car_x = (float)x;
    out->car_y = (float)y;
    out->car_rot = (float)map_angle_norm(f->car_hdg - out->bearing);
}

/* ---- commands ---- */

static void apply(cb_mapw_t *w, cb_map_t *m, const cmd_t *c)
{
    follow_t *f = &w->fl;
    switch(c->op) {
        case OP_MOVE: cb_map_move_by(m, c->a, c->b); break;
        case OP_GLIDE: cb_map_glide(m, c->a, c->b, c->c); break;
        case OP_SCALE: cb_map_scale_by(m, c->a, c->b, c->c, c->d); break;
        case OP_GESTURE:
            cb_map_gesture(m, c->a != 0);
            f->gesture = c->a != 0;
            if(!f->gesture && f->on) grip(w, m, REGRIP_MS);   /* a tap: back onto the car, smoothly */
            break;
        case OP_EASE: cb_map_ease_to(m, c->a, c->b, NAN, c->c); break;
        case OP_CAMERA:
            cb_map_ease_camera(m, c->a, c->b, c->c, c->d, c->e, c->f, c->g);
            if(c->f >= 0) f->pad_now = c->f;
            break;
        case OP_FIT:
            cb_map_fit(m, c->a, c->b, c->c, c->d, c->e, c->f);
            f->pad_now = 0;
            break;
        case OP_RESIZE:
            cb_map_resize(m, (uint32_t)c->a, (uint32_t)c->b);
            w->view_w = (uint32_t)c->a;
            w->view_h = (uint32_t)c->b;
            break;
        case OP_STYLE: cb_map_set_style(m, c->s); break;
        case OP_FOLLOW: follow_set(w, m, c); break;
        case OP_FOLLOW_ZOOM: f->zoom = fmin(20.0, fmax(2.0, f->zoom + c->a)); break;
    }
}

/* ---- frames ---- */

static void publish(cb_mapw_t *w, cb_map_t *m)
{
    uint32_t fw, fh, stride;
    if(!cb_map_take_pixels(m, &w->back, &w->back_cap, &fw, &fh, &stride)) return;
    const cb_map_tag_t *t = cb_map_frame_tag(m);
    pthread_mutex_lock(&w->mu);
    uint8_t *p = w->ready;
    size_t cap = w->ready_cap;
    w->ready = w->back;
    w->ready_cap = w->back_cap;
    w->back = p;
    w->back_cap = cap;
    w->w = fw;
    w->h = fh;
    w->stride = stride;
    w->ready_seq++;
    w->tag = *t;
    w->tag_seq++;
    pthread_mutex_unlock(&w->mu);
    w->st_frames++;
}

/* The map stood still and only the car moved: the frame on screen is still right, so move just the puck. */
static void publish_tag(cb_mapw_t *w, cb_map_t *m)
{
    cb_map_tag_t t = { 0 };
    tag_car(m, w, &t);
    pthread_mutex_lock(&w->mu);
    if(t.car_on != w->tag.car_on || fabsf(t.car_x - w->tag.car_x) > 0.05f || fabsf(t.car_y - w->tag.car_y) > 0.05f ||
       fabsf(t.car_rot - w->tag.car_rot) > 0.05f) {
        w->tag = t;
        w->tag_seq++;
        w->st_tags++;
    }
    pthread_mutex_unlock(&w->mu);
}

static void apply_sources(cb_mapw_t *w, cb_map_t *m)
{
    for(int i = 0; i < MAX_SOURCES; i++) {
        pthread_mutex_lock(&w->mu);
        char *json = w->src[i].json;
        char id[32];
        memcpy(id, w->src[i].id, sizeof id);
        w->src[i].json = NULL;
        pthread_mutex_unlock(&w->mu);
        if(!json) continue;
        if(cb_map_set_geojson(m, id, json)) {
            pthread_mutex_lock(&w->mu);
            free(w->src[i].last);
            w->src[i].last = json;
            pthread_mutex_unlock(&w->mu);
            continue;
        }
        pthread_mutex_lock(&w->mu);   /* not accepted yet: keep it unless a newer one arrived */
        if(!w->src[i].json) w->src[i].json = json;
        else free(json);
        pthread_mutex_unlock(&w->mu);
    }
}

static void *run(void *arg)
{
    cb_mapw_t *w = arg;
    cb_map_t *m = cb_map_create(&w->cfg);
    if(!m) {
        pthread_mutex_lock(&w->mu);
        w->failed = true;
        snprintf(w->why, sizeof w->why, "%s", cb_map_last_error());
        pthread_mutex_unlock(&w->mu);
        return NULL;
    }
    cb_map_set_tag_fn(m, tag_car, w);
    bool styled = false;
    double next_render = 0;
    for(;;) {
        pthread_mutex_lock(&w->mu);
        if(w->stop) {
            pthread_mutex_unlock(&w->mu);
            break;
        }
        cmd_t *list = w->head;
        w->head = w->tail = NULL;
        pthread_mutex_unlock(&w->mu);
        uint32_t period = atomic_load(&w->period_ms);

        while(list) {
            cmd_t *n = list->next;
            apply(w, m, list);
            if(list->op == OP_STYLE) {   /* the new style drops our layers: add them and the overlays again */
                styled = false;
                pthread_mutex_lock(&w->mu);
                for(int i = 0; i < MAX_SOURCES; i++)
                    if(w->src[i].last && !w->src[i].json) w->src[i].json = strdup(w->src[i].last);
                pthread_mutex_unlock(&w->mu);
            }
            free(list->s);
            free(list);
            list = n;
        }
        if(!styled && cb_map_style_ready(m)) {
            if(w->on_style) w->on_style(m);
            styled = true;
        }
        if(styled) apply_sources(w, m);

        double now = now_ms();
        if(period && now >= next_render) {
            follow_t *f = &w->fl;
            pthread_mutex_lock(&w->mu);
            map_track_t track = w->track;
            pthread_mutex_unlock(&w->mu);
            f->have_car = map_track_at(&track, now, &f->car_lat, &f->car_lon, &f->car_hdg);
            if(f->on && !f->gesture && f->have_car) follow_frame(w, m, now);
            if(cb_map_step(m)) publish(w, m);
            else if(f->have_car && cb_map_idle(m)) publish_tag(w, m);
            stats_tick(w, m, now);
            next_render = now + period;
        }
        else cb_map_pump(m);

        double wait = period == 0 ? 250 : next_render - now_ms();
        if(wait < 1) wait = 1;
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        long ns = ts.tv_nsec + (long)(wait * 1e6);
        ts.tv_sec += ns / 1000000000L;
        ts.tv_nsec = ns % 1000000000L;
        pthread_mutex_lock(&w->mu);
        if(!w->stop && !w->head) pthread_cond_timedwait(&w->cv, &w->mu, &ts);
        pthread_mutex_unlock(&w->mu);
    }
    cb_map_destroy(m);
    return NULL;
}

cb_mapw_t *cb_mapw_create(const cb_map_cfg_t *cfg, cb_mapw_style_cb_t on_style)
{
    cb_mapw_t *w = calloc(1, sizeof *w);
    if(!w) return NULL;
    w->cfg = *cfg;
    w->on_style = on_style;
    atomic_init(&w->period_ms, 33);
    w->view_w = cfg->width;
    w->view_h = cfg->height;
    w->fl.zoom = cfg->zoom;
    const char *st = getenv("CB_MAP_STATS");
    w->stats_on = st && *st && strcmp(st, "0") != 0;
    pthread_mutex_init(&w->mu, NULL);
    pthread_cond_init(&w->cv, NULL);
    if(pthread_create(&w->thread, NULL, run, w) != 0) {
        free(w);
        return NULL;
    }
    return w;
}

void cb_mapw_destroy(cb_mapw_t *w)
{
    if(!w) return;
    pthread_mutex_lock(&w->mu);
    w->stop = true;
    pthread_cond_signal(&w->cv);
    pthread_mutex_unlock(&w->mu);
    pthread_join(w->thread, NULL);
    while(w->head) {
        cmd_t *n = w->head->next;
        free(w->head->s);
        free(w->head);
        w->head = n;
    }
    for(int i = 0; i < MAX_SOURCES; i++) {
        free(w->src[i].json);
        free(w->src[i].last);
    }
    free(w->back);
    free(w->ready);
    free(w->front);
    pthread_cond_destroy(&w->cv);
    pthread_mutex_destroy(&w->mu);
    free(w);
}

bool cb_mapw_failed(cb_mapw_t *w, char *why, size_t cap)
{
    pthread_mutex_lock(&w->mu);
    bool f = w->failed;
    if(f) snprintf(why, cap, "%s", w->why);
    pthread_mutex_unlock(&w->mu);
    return f;
}

const uint8_t *cb_mapw_take(cb_mapw_t *w, cb_mapw_frame_t *f)
{
    const uint8_t *out = NULL;
    pthread_mutex_lock(&w->mu);
    if(w->ready_seq != w->taken_seq) {
        uint8_t *p = w->front;
        size_t cap = w->front_cap;
        w->front = w->ready;
        w->front_cap = w->ready_cap;
        w->ready = p;
        w->ready_cap = cap;
        w->taken_seq = w->ready_seq;
        f->w = w->w;
        f->h = w->h;
        f->stride = w->stride;
        out = w->front;
    }
    f->tag = w->tag;
    f->tag_seq = w->tag_seq;
    pthread_mutex_unlock(&w->mu);
    return out;
}

void cb_mapw_set_period(cb_mapw_t *w, uint32_t ms)
{
    uint32_t was = atomic_load(&w->period_ms);
    if(was == ms) return;   /* called every UI tick: the lock only when the pace changes */
    atomic_store(&w->period_ms, ms);
    if(ms && (was == 0 || ms < was)) {
        pthread_mutex_lock(&w->mu);
        pthread_cond_signal(&w->cv);
        pthread_mutex_unlock(&w->mu);
    }
}

void cb_mapw_move_by(cb_mapw_t *w, double dx, double dy) { push(w, (cmd_t){ .op = OP_MOVE, .a = dx, .b = dy }); }

void cb_mapw_glide(cb_mapw_t *w, double dx, double dy, double ms)
{
    push(w, (cmd_t){ .op = OP_GLIDE, .a = dx, .b = dy, .c = ms });
}

void cb_mapw_scale_by(cb_mapw_t *w, double s, double ax, double ay, double ms)
{
    push(w, (cmd_t){ .op = OP_SCALE, .a = s, .b = ax, .c = ay, .d = ms });
}

void cb_mapw_gesture(cb_mapw_t *w, bool on) { push(w, (cmd_t){ .op = OP_GESTURE, .a = on }); }

void cb_mapw_ease_to(cb_mapw_t *w, double lat, double lon, double ms)
{
    push(w, (cmd_t){ .op = OP_EASE, .a = lat, .b = lon, .c = ms });
}

void cb_mapw_camera_to(cb_mapw_t *w, double lat, double lon, double zoom, double bearing, double pitch,
                       double pad_top_px, double ms)
{
    push(w, (cmd_t){ .op = OP_CAMERA, .a = lat, .b = lon, .c = zoom, .d = bearing, .e = pitch, .f = pad_top_px,
                     .g = ms });
}

void cb_mapw_fit(cb_mapw_t *w, double south, double west, double north, double east, double pad_px, double ms)
{
    push(w, (cmd_t){ .op = OP_FIT, .a = south, .b = west, .c = north, .d = east, .e = pad_px, .f = ms });
}

double cb_mapw_bearing(cb_mapw_t *w)
{
    pthread_mutex_lock(&w->mu);
    double b = w->tag.bearing;
    pthread_mutex_unlock(&w->mu);
    return b;
}

void cb_mapw_resize(cb_mapw_t *w, uint32_t width, uint32_t height)
{
    push(w, (cmd_t){ .op = OP_RESIZE, .a = width, .b = height });
}

void cb_mapw_set_style(cb_mapw_t *w, const char *url)
{
    char *copy = strdup(url);
    if(copy) push(w, (cmd_t){ .op = OP_STYLE, .s = copy });
}

void cb_mapw_set_geojson(cb_mapw_t *w, const char *id, const char *json)
{
    char *copy = strdup(json);
    if(!copy) return;
    pthread_mutex_lock(&w->mu);
    int slot = -1;
    for(int i = 0; i < MAX_SOURCES && slot < 0; i++)
        if(!strcmp(w->src[i].id, id)) slot = i;
    for(int i = 0; i < MAX_SOURCES && slot < 0; i++)
        if(!w->src[i].id[0]) slot = i;
    if(slot >= 0) {
        snprintf(w->src[slot].id, sizeof w->src[slot].id, "%s", id);
        free(w->src[slot].json);
        w->src[slot].json = copy;
        pthread_cond_signal(&w->cv);
    }
    else free(copy);
    pthread_mutex_unlock(&w->mu);
}

void cb_mapw_set_car(cb_mapw_t *w, double lat, double lon, double heading)
{
    pthread_mutex_lock(&w->mu);
    map_track_push(&w->track, lat, lon, heading, now_ms());
    pthread_mutex_unlock(&w->mu);
}

void cb_mapw_follow(cb_mapw_t *w, bool on, bool heading_up, double zoom, double pitch, double pad_top_px,
                    double ease_ms)
{
    push(w, (cmd_t){ .op = OP_FOLLOW, .a = on, .b = heading_up, .c = zoom, .d = pitch, .e = pad_top_px,
                     .f = ease_ms });
}

void cb_mapw_follow_zoom(cb_mapw_t *w, double delta_levels)
{
    push(w, (cmd_t){ .op = OP_FOLLOW_ZOOM, .a = delta_levels });
}
