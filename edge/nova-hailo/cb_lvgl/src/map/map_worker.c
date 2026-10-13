#include "map/map_worker.h"
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_SOURCES 4

typedef enum { OP_MOVE, OP_GLIDE, OP_SCALE, OP_GESTURE, OP_EASE, OP_CAMERA, OP_FIT, OP_RESIZE, OP_STYLE } op_t;

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

struct cb_mapw {
    pthread_t thread;
    pthread_mutex_t mu;
    pthread_cond_t cv;
    bool stop, failed;
    char why[160];
    cb_map_cfg_t cfg;
    cb_mapw_style_cb_t on_style;
    cmd_t *head, *tail;
    uint32_t period_ms;
    source_t src[MAX_SOURCES];
    /* frames: the worker fills `ready`; the UI swaps it with `front`, which the worker never touches */
    uint8_t *ready, *front;
    size_t ready_cap, front_cap;
    uint32_t w, h, stride;
    uint64_t ready_seq, taken_seq;
    double bearing;   /* of the last rendered frame */
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

static void apply(cb_map_t *m, const cmd_t *c)
{
    switch(c->op) {
        case OP_MOVE: cb_map_move_by(m, c->a, c->b); break;
        case OP_GLIDE: cb_map_glide(m, c->a, c->b, c->c); break;
        case OP_SCALE: cb_map_scale_by(m, c->a, c->b, c->c, c->d); break;
        case OP_GESTURE: cb_map_gesture(m, c->a != 0); break;
        case OP_EASE: cb_map_ease_to(m, c->a, c->b, NAN, c->c); break;
        case OP_CAMERA: cb_map_ease_camera(m, c->a, c->b, c->c, c->d, c->e, c->f, c->g); break;
        case OP_FIT: cb_map_fit(m, c->a, c->b, c->c, c->d, c->e, c->f); break;
        case OP_RESIZE: cb_map_resize(m, (uint32_t)c->a, (uint32_t)c->b); break;
        case OP_STYLE: cb_map_set_style(m, c->s); break;
    }
}

static void publish(cb_mapw_t *w, cb_map_t *m)
{
    uint32_t fw, fh, stride;
    const uint8_t *px = cb_map_pixels(m, &fw, &fh, &stride);
    if(!px) return;
    size_t bytes = (size_t)stride * fh;
    pthread_mutex_lock(&w->mu);
    if(w->ready_cap < bytes) {
        uint8_t *p = realloc(w->ready, bytes);
        if(!p) {
            pthread_mutex_unlock(&w->mu);
            return;
        }
        w->ready = p;
        w->ready_cap = bytes;
    }
    memcpy(w->ready, px, bytes);
    w->w = fw;
    w->h = fh;
    w->stride = stride;
    w->ready_seq++;
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
        uint32_t period = w->period_ms;
        pthread_mutex_unlock(&w->mu);

        while(list) {
            cmd_t *n = list->next;
            apply(m, list);
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
            if(cb_map_step(m)) {
                publish(w, m);
                double br;
                if(cb_map_camera(m, NULL, NULL, NULL, &br)) {
                    pthread_mutex_lock(&w->mu);
                    w->bearing = br;
                    pthread_mutex_unlock(&w->mu);
                }
            }
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
    w->period_ms = 33;
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

const uint8_t *cb_mapw_take(cb_mapw_t *w, uint32_t *width, uint32_t *height, uint32_t *stride)
{
    pthread_mutex_lock(&w->mu);
    if(w->ready_seq == w->taken_seq) {
        pthread_mutex_unlock(&w->mu);
        return NULL;
    }
    uint8_t *p = w->front;
    size_t cap = w->front_cap;
    w->front = w->ready;
    w->front_cap = w->ready_cap;
    w->ready = p;
    w->ready_cap = cap;
    w->taken_seq = w->ready_seq;
    *width = w->w;
    *height = w->h;
    *stride = w->stride;
    const uint8_t *out = w->front;
    pthread_mutex_unlock(&w->mu);
    return out;
}

void cb_mapw_set_period(cb_mapw_t *w, uint32_t ms)
{
    pthread_mutex_lock(&w->mu);
    bool sooner = ms && (w->period_ms == 0 || ms < w->period_ms);
    w->period_ms = ms;
    if(sooner) pthread_cond_signal(&w->cv);
    pthread_mutex_unlock(&w->mu);
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
    double b = w->bearing;
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
