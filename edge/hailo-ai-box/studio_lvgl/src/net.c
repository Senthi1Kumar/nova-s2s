#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "net.h"
#include "json.h"
#include "ws.h"
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define PREVIEW_MAX_BYTES (1280 * 720 * 2)

static struct {
    char host[128], key[96], base[160];
    int port;
    pthread_mutex_t mu;
    pthread_t th_net, th_http;
    volatile bool stop, started;

    net_snapshot_t snap;

    /* vision: UI -> net */
    bool v_want;
    uint32_t v_cfg_gen;
    char v_source[48], v_task[16];
    int v_fps, v_pw;
    /* vision: net -> UI */
    net_vision_t vis;
    uint8_t *vbuf[3];   /* back (net writes), ready (latest complete), front (UI reads) */
    int v_back, v_ready, v_front;
    bool v_ready_new;
    uint32_t v_last_preview_ms;
    int v_pending_w, v_pending_h;

    /* chat */
    char *c_req;        /* JSON to send, owned by net thread once taken */
    bool c_cancel;
    net_chat_t chat;
    uint32_t c_t0;

    /* RealSense ID: one queued operation at a time on the HTTP thread */
    int f_op;               /* 1 authenticate, 2 enroll, 3 delete one, 4 delete all, 5 refresh */
    char f_name[32], f_role[12], f_user_id[32];
    bool f_confirm;
    net_face_t face;
} G = { .mu = PTHREAD_MUTEX_INITIALIZER };

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static void cpy(char *dst, size_t cap, const char *src)
{
    snprintf(dst, cap, "%s", src ? src : "");
}

static void with_key(const char *path, char *out, size_t cap)
{
    if(G.key[0]) snprintf(out, cap, "%s%skey=%s", path, strchr(path, '?') ? "&" : "?", G.key);
    else snprintf(out, cap, "%s", path);
}

static void ws_url(const char *path, char *out, size_t cap)
{
    char p[200];
    with_key(path, p, sizeof p);
    snprintf(out, cap, "ws://%.120s:%d%.200s", G.host, G.port, p);
}

void net_ws_url(const char *path, char *out, size_t cap) { ws_url(path, out, cap); }

/* ---------- JSON helpers ---------- */

static double num(const cbj_t *o, const char *k, double d) { return cbj_num(cbj_get(o, k), d); }

static void fmt_time(double t, char out[12])
{
    time_t tt = (time_t)t;
    struct tm tm;
    if(t <= 0) tt = time(NULL);
    localtime_r(&tt, &tm);
    strftime(out, 12, "%H:%M:%S", &tm);
}

/* "k=v k=v" of an object's members (one level), for the event log */
static void flatten(const cbj_t *o, char *out, size_t cap)
{
    out[0] = '\0';
    if(!o) return;
    if(o->type == CBJ_STR) { cpy(out, cap, o->s); return; }
    if(o->type == CBJ_NUM) { snprintf(out, cap, "%g", o->n); return; }
    if(o->type != CBJ_OBJ) return;
    size_t n = 0;
    for(const cbj_t *c = o->child; c && n + 2 < cap; c = c->next) {
        int w;
        if(c->type == CBJ_NUM) w = snprintf(out + n, cap - n, "%s%s=%g", n ? " " : "", c->key, c->n);
        else if(c->type == CBJ_STR) w = snprintf(out + n, cap - n, "%s%s=%s", n ? " " : "", c->key, c->s);
        else if(c->type == CBJ_BOOL) w = snprintf(out + n, cap - n, "%s%s=%s", n ? " " : "", c->key, c->b ? "true" : "false");
        else continue;
        if(w < 0) break;
        n += (size_t)w;
    }
}

static void push_event_locked(const char *line)
{
    net_snapshot_t *s = &G.snap;
    for(int i = 0; i < s->n_events && i < 12; i++)
        if(!strcmp(s->events[i], line)) return;   /* the same event seen on the stream and in /recent */
    int n = s->n_events < NET_MAX_EVENTS ? s->n_events : NET_MAX_EVENTS - 1;
    memmove(s->events[1], s->events[0], (size_t)n * sizeof s->events[0]);
    cpy(s->events[0], sizeof s->events[0], line);
    s->n_events = n + 1;
    s->ev_seq++;
}

static void on_event_json(const cbj_t *o)
{
    char t[12], detail[80], line[120];
    fmt_time(num(o, "t", 0), t);
    flatten(cbj_get(o, "detail"), detail, sizeof detail);
    snprintf(line, sizeof line, "%s  %s  %s", t, cbj_str(cbj_get(o, "event"), "event"), detail);
    pthread_mutex_lock(&G.mu);
    push_event_locked(line);
    pthread_mutex_unlock(&G.mu);
}

static void parse_row(const cbj_t *o, net_row_t *r)
{
    memset(r, 0, sizeof *r);
    fmt_time(num(o, "t", 0), r->t);
    cpy(r->client, sizeof r->client, cbj_str(cbj_get(o, "client"), "-"));
    cpy(r->endpoint, sizeof r->endpoint, cbj_str(cbj_get(o, "endpoint"), "-"));
    cpy(r->model, sizeof r->model, cbj_str(cbj_get(o, "model"), "-"));
    cpy(r->device, sizeof r->device, cbj_str(cbj_get(o, "device"), "-"));
    r->queue_ms = num(o, "queue_ms", 0);
    r->infer_ms = num(o, "infer_ms", 0);
    r->status = (int)num(o, "status", 0);
}

/* newest-first insert of one request row (stream push) */
static void push_row_locked(const net_row_t *r)
{
    net_snapshot_t *s = &G.snap;
    int n = s->n_rows < NET_MAX_ROWS ? s->n_rows : NET_MAX_ROWS - 1;
    memmove(&s->rows[1], &s->rows[0], (size_t)n * sizeof s->rows[0]);
    s->rows[0] = *r;
    s->n_rows = n + 1;
    s->rows_seq++;
}

static void parse_metrics(const cbj_t *root)
{
    const cbj_t *host = cbj_get(root, "host"), *npu = cbj_get(root, "npu"), *srv = cbj_get(root, "server");
    net_snapshot_t *s = &G.snap;
    s->cpu_pct = num(host, "cpu_pct", 0);
    const cbj_t *pc = cbj_get(host, "cpu_per_core");
    s->ncores = 0;
    for(int i = 0; pc && i < pc->count && i < NET_MAX_CORES; i++) s->core[s->ncores++] = cbj_num(cbj_at(pc, i), 0);
    s->mem_used = num(host, "mem_used_mb", 0);
    s->mem_total = num(host, "mem_total_mb", 0);
    s->temp_c = num(host, "temp_c", 0);
    s->arm_mhz = num(host, "arm_mhz", 0);
    s->load1 = num(host, "load_1m", 0);
    cpy(s->throttled, sizeof s->throttled, cbj_str(cbj_get(host, "throttled"), "0x0"));
    s->npu_util = num(npu, "nnc_util_pct", 0);
    s->npu_ram_used = num(npu, "ram_used_mb", 0);
    s->npu_ram_total = num(npu, "ram_total_mb", 0);
    s->npu_temp = num(npu, "temp_c", 0);
    cpy(s->genai_owner, sizeof s->genai_owner, cbj_str(cbj_get(npu, "genai_owner"), ""));
    cpy(s->genai_state, sizeof s->genai_state, cbj_str(cbj_get(npu, "genai_state"), ""));
    const cbj_t *ms = cbj_get(root, "models");
    s->n_ms = 0;
    for(int i = 0; ms && i < ms->count && s->n_ms < NET_MAX_MSTAT; i++) {
        const cbj_t *m = cbj_at(ms, i);
        net_mstat_t *d = &s->ms[s->n_ms++];
        cpy(d->id, sizeof d->id, cbj_str(cbj_get(m, "id"), "?"));
        cpy(d->state, sizeof d->state, cbj_str(cbj_get(m, "state"), "-"));
        d->fps = num(m, "fps", 0);
        d->p50 = num(m, "p50_ms", 0);
        d->p95 = num(m, "p95_ms", 0);
        d->requests = (long)num(m, "requests", 0);
    }
    s->rps = num(srv, "rps", 0);
    s->clients = (int)num(srv, "clients", 0);
    s->req_total = num(srv, "requests_total", 0);
    s->uptime = num(srv, "uptime_s", 0);
    s->seq++;
}

static void on_system_text(void *ud, const char *text, size_t len)
{
    (void)ud;
    cbj_t *root = cbj_parse(text, len);
    if(!root) return;
    const cbj_t *r = root;
    if(!cbj_get(r, "host") && cbj_get(r, "metrics")) r = cbj_get(r, "metrics");
    if(!cbj_get(r, "host") && cbj_get(r, "data")) r = cbj_get(r, "data");
    if(cbj_get(r, "host")) {
        pthread_mutex_lock(&G.mu);
        parse_metrics(r);
        pthread_mutex_unlock(&G.mu);
    }
    else if(!strcmp(cbj_str(cbj_get(root, "kind"), ""), "event") || cbj_get(root, "event")) on_event_json(root);
    else if(cbj_get(root, "endpoint")) {
        net_row_t row;
        parse_row(root, &row);
        pthread_mutex_lock(&G.mu);
        push_row_locked(&row);
        pthread_mutex_unlock(&G.mu);
    }
    cbj_free(root);
}

/* ---------- vision ---------- */

static uintptr_t s_vconn;   /* id of the live vision socket; callbacks of any other (closing) socket are ignored */

static void vis_text(void *ud, const char *text, size_t len)
{
    if((uintptr_t)ud != s_vconn) return;
    cbj_t *root = cbj_parse(text, len);
    if(!root) return;
    const char *type = cbj_str(cbj_get(root, "type"), "");
    if(!strcmp(type, "preview")) {
        G.v_pending_w = (int)num(root, "w", 0);
        G.v_pending_h = (int)num(root, "h", 0);
        cbj_free(root);
        return;
    }
    if(!strcmp(type, "status")) {
        pthread_mutex_lock(&G.mu);
        if(!strcmp(cbj_str(cbj_get(root, "state"), ""), "loading")) cpy(G.vis.loading, sizeof G.vis.loading, cbj_str(cbj_get(root, "model"), "model"));
        else G.vis.loading[0] = '\0';
        pthread_mutex_unlock(&G.mu);
        cbj_free(root);
        return;
    }
    if(!strcmp(type, "error") || cbj_get(root, "error")) {
        const cbj_t *e = cbj_get(root, "error");
        char msg[96];
        if(e && e->type == CBJ_OBJ) cpy(msg, sizeof msg, cbj_str(cbj_get(e, "message"), "error"));
        else cpy(msg, sizeof msg, cbj_str(e, "error"));
        pthread_mutex_lock(&G.mu);
        cpy(G.vis.err, sizeof G.vis.err, msg);
        G.vis.loading[0] = '\0';
        pthread_mutex_unlock(&G.mu);
        cbj_free(root);
        return;
    }
    const cbj_t *res = cbj_get(root, "results");
    if(!res) { cbj_free(root); return; }
    pthread_mutex_lock(&G.mu);
    net_vision_t *v = &G.vis;
    v->err[0] = '\0';
    v->loading[0] = '\0';
    v->img_w = (int)num(res, "image_width", 0);
    v->img_h = (int)num(res, "image_height", 0);
    v->n_boxes = v->n_lanes = v->n_kp = v->n_face_lm = v->n_masks = 0;
    v->face_held = false;
    bool dms_task = cbj_get(res, "dms") != NULL;
    const cbj_t *dets = cbj_get(res, "detections");
    v->n_det = dets ? dets->count : 0;
    for(int i = 0; dets && i < dets->count; i++) {
        const cbj_t *d = cbj_at(dets, i), *bb = cbj_get(d, "bbox");
        int box_idx = -1;
        if(dms_task && v->n_boxes >= 1) bb = NULL;   /* Driver Monitoring: only the driver face box */
        if(bb && bb->count >= 4 && v->n_boxes < NET_MAX_BOXES) {
            net_box_t *b = &v->boxes[v->n_boxes++];
            box_idx = v->n_boxes - 1;
            b->x1 = (float)cbj_num(cbj_at(bb, 0), 0);
            b->y1 = (float)cbj_num(cbj_at(bb, 1), 0);
            b->x2 = (float)cbj_num(cbj_at(bb, 2), 0);
            b->y2 = (float)cbj_num(cbj_at(bb, 3), 0);
            snprintf(b->label, sizeof b->label, "%s %.0f%%", cbj_str(cbj_get(d, "class"), "?"), num(d, "confidence", 0) * 100);
        }
        const cbj_t *rle = cbj_get(d, "mask_rle");
        if(box_idx >= 0 && rle && v->n_masks < NET_MAX_MASKS) {
            const cbj_t *size = cbj_get(rle, "size"), *counts = cbj_get(rle, "counts");
            if(size && size->count >= 2 && counts && counts->count <= NET_MASK_RUNS) {
                net_mask_t *m = &v->masks[v->n_masks++];
                m->w = (int)cbj_num(cbj_at(size, 1), 0);
                m->h = (int)cbj_num(cbj_at(size, 0), 0);
                m->box = box_idx;
                m->n_runs = counts->count;
                for(int k = 0; k < counts->count; k++) m->counts[k] = (uint32_t)cbj_num(cbj_at(counts, k), 0);
            }
        }
        if(i == 0) {
            const cbj_t *lm = cbj_get(d, "landmarks");
            for(int k = 0; lm && k < lm->count && v->n_face_lm < 5; k++) {
                const cbj_t *p = cbj_at(lm, k);
                if(!p || p->count < 2) continue;
                v->face_lm[v->n_face_lm][0] = (float)cbj_num(cbj_at(p, 0), 0);
                v->face_lm[v->n_face_lm][1] = (float)cbj_num(cbj_at(p, 1), 0);
                v->n_face_lm++;
            }
            v->face_held = cbj_bool(cbj_get(d, "held"), false);
        }
        const cbj_t *kp = cbj_get(d, "keypoints");
        for(int k = 0; kp && k < kp->count && v->n_kp < NET_MAX_KP; k++) {
            const cbj_t *p = cbj_at(kp, k);
            if(!p || p->count < 2 || (p->count >= 3 && cbj_num(cbj_at(p, 2), 1) < 0.3)) continue;
            v->kp[v->n_kp][0] = (float)cbj_num(cbj_at(p, 0), 0);
            v->kp[v->n_kp][1] = (float)cbj_num(cbj_at(p, 1), 0);
            v->n_kp++;
        }
    }
    const cbj_t *txt = cbj_get(res, "text");
    for(int i = 0; txt && i < txt->count; i++) {
        const cbj_t *d = cbj_at(txt, i), *bb = cbj_get(d, "bbox");
        if(bb && bb->count >= 4 && v->n_boxes < NET_MAX_BOXES) {
            net_box_t *b = &v->boxes[v->n_boxes++];
            b->x1 = (float)cbj_num(cbj_at(bb, 0), 0);
            b->y1 = (float)cbj_num(cbj_at(bb, 1), 0);
            b->x2 = (float)cbj_num(cbj_at(bb, 2), 0);
            b->y2 = (float)cbj_num(cbj_at(bb, 3), 0);
            cpy(b->label, sizeof b->label, cbj_str(cbj_get(d, "text"), ""));
        }
    }
    if(txt) v->n_det = txt->count;
    const cbj_t *lanes = cbj_get(res, "lanes");
    for(int i = 0; lanes && i < lanes->count && v->n_lanes < NET_MAX_LANES; i++) {
        const cbj_t *ln = cbj_at(lanes, i);
        net_poly_t *p = &v->lanes[v->n_lanes++];
        p->n = 0;
        for(int k = 0; ln && k < ln->count && p->n < NET_LANE_PTS; k++) {
            const cbj_t *pt = cbj_at(ln, k);
            p->x[p->n] = (float)cbj_num(cbj_at(pt, 0), 0);
            p->y[p->n] = (float)cbj_num(cbj_at(pt, 1), 0);
            p->n++;
        }
    }
    v->info[0] = v->dms_status[0] = v->dms_summary[0] = '\0';
    const cbj_t *dms = cbj_get(res, "dms");
    if(dms) {
        snprintf(v->info, sizeof v->info, "faces %d  yaw %.0f deg  phone %s  eyes visible %s  eyes on road %s", (int)num(dms, "faces", 0),
                 num(dms, "head_yaw_deg", 0), cbj_bool(cbj_get(dms, "phone_in_hand"), false) ? "yes" : "no",
                 cbj_bool(cbj_get(dms, "eyes_visible"), true) ? "yes" : "no", cbj_bool(cbj_get(dms, "eyes_on_road"), true) ? "yes" : "no");
        cpy(v->dms_status, sizeof v->dms_status, cbj_str(cbj_get(dms, "status"), ""));
        cpy(v->dms_summary, sizeof v->dms_summary, cbj_str(cbj_get(dms, "summary"), ""));
        if(!v->dms_summary[0]) cpy(v->dms_summary, sizeof v->dms_summary, "Driver monitoring running");
    }
    {
        const cbj_t *vt = cbj_get(root, "video_t");
        if(!vt) vt = cbj_get(res, "video_t");
        v->video_t = vt ? cbj_num(vt, -1) : -1;
    }
    v->infer_ms = num(res, "inference_ms", 0);
    v->fps = num(root, "fps", 0);
    cpy(v->task, sizeof v->task, cbj_str(cbj_get(root, "task"), cbj_str(cbj_get(res, "task"), "")));
    cpy(v->degraded, sizeof v->degraded, cbj_str(cbj_get(root, "degraded"), cbj_str(cbj_get(res, "degraded"), "")));
    v->e2e_ms = G.v_last_preview_ms ? (double)(now_ms() - G.v_last_preview_ms) : 0;
    v->rgen++;
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
}

static void vis_bin(void *ud, const uint8_t *data, size_t len)
{
    if((uintptr_t)ud != s_vconn) return;
    int w = G.v_pending_w, h = G.v_pending_h;
    if(w <= 0 || h <= 0 || (size_t)w * (size_t)h * 2 != len || len > PREVIEW_MAX_BYTES) return;   /* jpeg or malformed: ignore */
    memcpy(G.vbuf[G.v_back], data, len);   /* back is the net thread's alone */
    pthread_mutex_lock(&G.mu);
    int t = G.v_ready;
    G.v_ready = G.v_back;
    G.v_back = t;
    G.v_ready_new = true;
    G.vis.w = w;
    G.vis.h = h;
    G.vis.pgen++;
    G.v_last_preview_ms = now_ms();
    pthread_mutex_unlock(&G.mu);
}

static ws_client_t *s_vws, *s_vold;
static uintptr_t s_vconn_seq;
#define VOLD_WAIT_MS 2500   /* longest wait for the old stream's socket to close */
#define VSTART_GAP_MS 400   /* breathing room after it closed, for the server to release the camera */
static void vis_open(void *ud)
{
    if((uintptr_t)ud != s_vconn) return;
    char msg[300];
    pthread_mutex_lock(&G.mu);
    char src[160];
    if(!strncmp(G.v_source, "video:", 6)) snprintf(src, sizeof src, "\"source\":\"video\",\"video_id\":\"%.47s\"", G.v_source + 6);
    else snprintf(src, sizeof src, "\"source\":\"%.47s\"", G.v_source);
    int n = snprintf(msg, sizeof msg, "{%s,\"task\":\"%s\",\"fps\":%d,\"preview\":\"rgb565\",\"preview_width\":%d}", src, G.v_task,
                     G.v_fps, G.v_pw);
    G.v_pending_w = G.v_pending_h = 0;
    pthread_mutex_unlock(&G.mu);
    ws_client_send(s_vws, WS_OP_TEXT, msg, (size_t)n);
}

/* ---------- chat ---------- */

static ws_client_t *s_cws;

static void chat_finish_locked(bool ok, const char *err)
{
    G.chat.streaming = false;
    G.chat.done = ok;
    G.chat.error = !ok;
    if(!ok) cpy(G.chat.err, sizeof G.chat.err, err);
    G.chat.gen++;
}

static void chat_text(void *ud, const char *text, size_t len)
{
    (void)ud;
    cbj_t *root = cbj_parse(text, len);
    if(!root) return;
    const char *type = cbj_str(cbj_get(root, "type"), "");
    pthread_mutex_lock(&G.mu);
    net_chat_t *c = &G.chat;
    if(!strcmp(type, "token")) {
        const char *t = cbj_str(cbj_get(root, "text"), "");
        size_t n = strlen(t), have = strlen(c->text);
        if(have + n + 1 < NET_CHAT_CAP) memcpy(c->text + have, t, n + 1);
        if(c->tokens == 0) c->ttft_ms = (double)(now_ms() - G.c_t0);
        c->tokens++;
        c->gen++;
    }
    else if(!strcmp(type, "done")) {
        const cbj_t *u = cbj_get(root, "usage");
        double ttft = num(root, "ttft_ms", 0);
        if(ttft > 0) c->ttft_ms = ttft;
        c->tps = num(root, "tokens_per_s", 0);
        if(u && num(u, "completion_tokens", 0) > 0) c->tokens = (int)num(u, "completion_tokens", 0);
        chat_finish_locked(true, "");
    }
    else if(!strcmp(type, "error") || cbj_get(root, "error")) {
        const cbj_t *e = cbj_get(root, "error");
        char msg[160];
        if(e && e->type == CBJ_OBJ) cpy(msg, sizeof msg, cbj_str(cbj_get(e, "message"), "error"));
        else cpy(msg, sizeof msg, cbj_str(e, "error"));
        chat_finish_locked(false, msg);
    }
    else if(!strcmp(type, "swap") || !strcmp(type, "status")) {
        cpy(c->note, sizeof c->note, cbj_str(cbj_get(root, "text"), cbj_str(cbj_get(root, "message"), "loading model...")));
        c->gen++;
    }
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
}

static void chat_open(void *ud)
{
    (void)ud;
    pthread_mutex_lock(&G.mu);
    char *req = G.c_req;
    G.c_req = NULL;
    pthread_mutex_unlock(&G.mu);
    if(req) {
        ws_client_send(s_cws, WS_OP_TEXT, req, strlen(req));
        free(req);
    }
}

static void json_str(char **buf, size_t *n, size_t *cap, const char *s)
{
    size_t need = *n + strlen(s) * 6 + 8;
    if(need > *cap) {
        *cap = need * 2;
        *buf = realloc(*buf, *cap);
    }
    char *b = *buf;
    b[(*n)++] = '"';
    for(const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if(*p == '"' || *p == '\\') { b[(*n)++] = '\\'; b[(*n)++] = (char)*p; }
        else if(*p == '\n') { b[(*n)++] = '\\'; b[(*n)++] = 'n'; }
        else if(*p == '\r' || *p == '\t') b[(*n)++] = ' ';
        else if(*p < 0x20) *n += (size_t)snprintf(b + *n, 7, "\\u%04x", *p);
        else b[(*n)++] = (char)*p;
    }
    b[(*n)++] = '"';
    b[*n] = '\0';
}

#define CHAT_IMAGE_MAX_BYTES (4 * 1024 * 1024)

static void raw_str(char **buf, size_t *n, size_t *cap, const char *s)
{
    size_t len = strlen(s), need = *n + len + 1;
    if(need > *cap) {
        *cap = need * 2;
        *buf = realloc(*buf, *cap);
    }
    memcpy(*buf + *n, s, len + 1);
    *n += len;
}

static char *image_data_uri(const char *path)
{
    FILE *f = fopen(path, "rb");
    if(!f) return NULL;
    if(fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long size = ftell(f);
    if(size <= 0 || size > CHAT_IMAGE_MAX_BYTES || fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    uint8_t *data = malloc((size_t)size);
    if(!data || fread(data, 1, (size_t)size, f) != (size_t)size) {
        free(data);
        fclose(f);
        return NULL;
    }
    fclose(f);
    static const char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const char *mime = strstr(path, ".png") ? "image/png" : "image/jpeg";
    const char *prefix = "data:";
    size_t prefix_n = strlen(prefix) + strlen(mime) + 8, out_n = 4 * ((size_t)size + 2) / 3;
    char *out = malloc(prefix_n + out_n + 1);
    if(!out) { free(data); return NULL; }
    int p = snprintf(out, prefix_n + 1, "%s%s;base64,", prefix, mime);
    size_t n = (size_t)p;
    for(long i = 0; i < size; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        if(i + 1 < size) v |= (uint32_t)data[i + 1] << 8;
        if(i + 2 < size) v |= data[i + 2];
        out[n++] = table[(v >> 18) & 63];
        out[n++] = table[(v >> 12) & 63];
        out[n++] = i + 1 < size ? table[(v >> 6) & 63] : '=';
        out[n++] = i + 2 < size ? table[v & 63] : '=';
    }
    out[n] = '\0';
    free(data);
    return out;
}

bool net_chat_send(const char *model, int n, const char *const *roles, const char *const *texts, int max_tokens, const char *image_path)
{
    pthread_mutex_lock(&G.mu);
    if(G.chat.streaming) {
        pthread_mutex_unlock(&G.mu);
        return false;
    }
    pthread_mutex_unlock(&G.mu);
    char *data_uri = image_path && image_path[0] ? image_data_uri(image_path) : NULL;
    if(image_path && image_path[0] && !data_uri) return false;
    size_t len = 0, cap = 512;
    char *b = malloc(cap);
    b[0] = '\0';
    len = (size_t)snprintf(b, cap, "{\"model\":");
    json_str(&b, &len, &cap, model);
    len += (size_t)snprintf(b + len, cap - len, ",\"max_tokens\":%d,\"messages\":[", max_tokens);
    for(int i = 0; i < n; i++) {
        if(len + 64 > cap) { cap *= 2; b = realloc(b, cap); }
        len += (size_t)snprintf(b + len, cap - len, "%s{\"role\":\"%s\",\"content\":", i ? "," : "", roles[i]);
        if(data_uri && i == n - 1) {
            raw_str(&b, &len, &cap, "[{\"type\":\"text\",\"text\":");
            json_str(&b, &len, &cap, texts[i]);
            raw_str(&b, &len, &cap, "},{\"type\":\"image_url\",\"image_url\":{\"url\":\"");
            raw_str(&b, &len, &cap, data_uri);
            raw_str(&b, &len, &cap, "\"}}]");
        }
        else json_str(&b, &len, &cap, texts[i]);
        if(len + 4 > cap) { cap *= 2; b = realloc(b, cap); }
        b[len++] = '}';
        b[len] = '\0';
    }
    free(data_uri);
    if(len + 4 > cap) b = realloc(b, len + 4);
    strcpy(b + len, "]}");
    pthread_mutex_lock(&G.mu);
    free(G.c_req);
    G.c_req = b;
    memset(&G.chat, 0, sizeof G.chat);
    G.chat.streaming = true;
    G.chat.gen++;
    G.c_t0 = now_ms();
    G.c_cancel = false;
    pthread_mutex_unlock(&G.mu);
    return true;
}

void net_chat_cancel(void)
{
    pthread_mutex_lock(&G.mu);
    G.c_cancel = true;
    pthread_mutex_unlock(&G.mu);
}

void net_chat_take(net_chat_t *out)
{
    pthread_mutex_lock(&G.mu);
    *out = G.chat;
    pthread_mutex_unlock(&G.mu);
}

static bool face_start(int op, const char *name, const char *role, const char *user_id, bool confirm)
{
    pthread_mutex_lock(&G.mu);
    if(G.face.busy || G.f_op) {
        pthread_mutex_unlock(&G.mu);
        return false;
    }
    net_face_user_t users[NET_MAX_FACE_USERS];
    memcpy(users, G.face.users, sizeof users);
    int n_users = G.face.n_users;
    uint32_t users_gen = G.face.users_gen;
    uint32_t next_gen = G.face.gen + 1;  /* never reuse a completed generation across operations */
    memset(&G.face, 0, sizeof G.face);
    memcpy(G.face.users, users, sizeof users);
    G.face.n_users = n_users;
    G.face.users_gen = users_gen;
    G.face.busy = op != 5;
    G.f_op = op;
    cpy(G.f_name, sizeof G.f_name, name);
    cpy(G.f_role, sizeof G.f_role, role);
    cpy(G.f_user_id, sizeof G.f_user_id, user_id);
    G.f_confirm = confirm;
    G.face.gen = next_gen;
    pthread_mutex_unlock(&G.mu);
    return true;
}

bool net_face_authenticate(void) { return face_start(1, "", "", "", false); }
bool net_face_enroll(const char *name, const char *role, bool confirm)
{
    return face_start(2, name, role, "", confirm);
}
bool net_face_delete(const char *user_id)
{
    return face_start(user_id && user_id[0] ? 3 : 4, "", "", user_id ? user_id : "", false);
}
bool net_face_refresh(void) { return face_start(5, "", "", "", false); }

void net_face_take(net_face_t *out)
{
    pthread_mutex_lock(&G.mu);
    *out = G.face;
    pthread_mutex_unlock(&G.mu);
}

/* ---------- vision control ---------- */

void net_vision_start(const char *source, const char *task, int fps, int preview_w)
{
    pthread_mutex_lock(&G.mu);
    cpy(G.v_source, sizeof G.v_source, source);
    cpy(G.v_task, sizeof G.v_task, task);
    G.v_fps = fps;
    G.v_pw = preview_w;
    G.v_want = true;
    G.v_cfg_gen++;
    G.vis.active = true;
    G.vis.err[0] = G.vis.loading[0] = G.vis.dms_status[0] = G.vis.dms_summary[0] = G.vis.info[0] = '\0';
    G.vis.video_t = -1;
    G.vis.n_boxes = G.vis.n_lanes = G.vis.n_kp = G.vis.n_det = G.vis.n_face_lm = G.vis.n_masks = 0;
    G.vis.face_held = false;
    G.vis.rgen++;
    G.v_ready_new = false;   /* a buffered frame belongs to the previous task or source */
    pthread_mutex_unlock(&G.mu);
}

void net_vision_stop(void)
{
    pthread_mutex_lock(&G.mu);
    G.v_want = false;
    G.v_cfg_gen++;
    G.vis.active = false;
    pthread_mutex_unlock(&G.mu);
}

void net_vision_take(net_vision_t *out)
{
    pthread_mutex_lock(&G.mu);
    if(G.v_ready_new) {
        int t = G.v_front;
        G.v_front = G.v_ready;
        G.v_ready = t;
        G.v_ready_new = false;
    }
    G.vis.pix = (const uint16_t *)G.vbuf[G.v_front];
    *out = G.vis;
    pthread_mutex_unlock(&G.mu);
}

void net_snapshot(net_snapshot_t *out)
{
    pthread_mutex_lock(&G.mu);
    *out = G.snap;
    pthread_mutex_unlock(&G.mu);
}

/* ---------- net thread ---------- */

static void *net_main(void *arg)
{
    (void)arg;
    char url[512];
    ws_url("/v1/system/stream", url, sizeof url);
    ws_client_t *sys = ws_client_create(url, on_system_text, NULL);
    uint32_t seen_cfg = 0, s_vold_deadline = 0, vold_done_ms = 0;
    bool chat_live = false, vstart_pending = false;
    while(!G.stop) {
        uint32_t now = now_ms();
        if(sys) {
            ws_state_t st = ws_client_poll(sys, now);
            pthread_mutex_lock(&G.mu);
            G.snap.online = st == WS_OPEN;
            cpy(G.snap.err, sizeof G.snap.err, st == WS_OPEN ? "" : ws_client_error(sys));
            pthread_mutex_unlock(&G.mu);
        }

        /* vision: reconcile the requested config */
        pthread_mutex_lock(&G.mu);
        uint32_t cfg = G.v_cfg_gen;
        bool want = G.v_want;
        pthread_mutex_unlock(&G.mu);
        if(cfg != seen_cfg) {
            seen_cfg = cfg;
            if(s_vws) {   /* close politely and wait for the server to drop it: a new stream must not start while the old one still holds the camera and the NPU */
                static const uint8_t k_close_1000[2] = { 0x03, 0xE8 };
                s_vconn = 0;
                ws_client_set_auto_reconnect(s_vws, false);
                ws_client_send(s_vws, WS_OP_CLOSE, k_close_1000, sizeof k_close_1000);
                s_vold = s_vws;
                s_vws = NULL;
                s_vold_deadline = now + VOLD_WAIT_MS;
            }
            vstart_pending = true;
        }
        if(s_vold && (ws_client_poll(s_vold, now) == WS_CLOSED || (int32_t)(now - s_vold_deadline) >= 0)) {
            ws_client_destroy(s_vold);
            s_vold = NULL;
            vold_done_ms = now;
        }
        if(vstart_pending && !s_vold && (int32_t)(now - vold_done_ms) >= VSTART_GAP_MS) {
            vstart_pending = false;
            pthread_mutex_lock(&G.mu);
            want = G.v_want;
            pthread_mutex_unlock(&G.mu);
            if(want) {
                ws_url("/v1/vision/stream", url, sizeof url);
                s_vconn = ++s_vconn_seq;
                s_vws = ws_client_create(url, vis_text, (void *)s_vconn);
                if(s_vws) {
                    ws_client_set_binary(s_vws, vis_bin);
                    ws_client_set_open(s_vws, vis_open);
                }
            }
        }
        if(s_vws) {
            ws_state_t st = ws_client_poll(s_vws, now);
            pthread_mutex_lock(&G.mu);
            G.vis.open = st == WS_OPEN;
            if(st != WS_OPEN && !G.vis.err[0]) cpy(G.vis.err, sizeof G.vis.err, ws_client_error(s_vws));
            pthread_mutex_unlock(&G.mu);
        }
        else {
            pthread_mutex_lock(&G.mu);
            G.vis.open = false;
            pthread_mutex_unlock(&G.mu);
        }

        /* chat: one connection per request */
        pthread_mutex_lock(&G.mu);
        bool cancel = G.c_cancel, have_req = G.c_req != NULL;
        if(cancel) G.c_cancel = false;
        pthread_mutex_unlock(&G.mu);
        if(cancel && s_cws) {
            ws_client_destroy(s_cws);
            s_cws = NULL;
            chat_live = false;
            pthread_mutex_lock(&G.mu);
            if(G.chat.streaming) chat_finish_locked(true, "");
            free(G.c_req);
            G.c_req = NULL;
            pthread_mutex_unlock(&G.mu);
        }
        if(have_req && !s_cws) {
            ws_url("/v1/chat/stream", url, sizeof url);
            s_cws = ws_client_create(url, chat_text, NULL);
            if(s_cws) {
                ws_client_set_open(s_cws, chat_open);
                ws_client_set_auto_reconnect(s_cws, false);
                chat_live = true;
            }
        }
        if(s_cws) {
            ws_state_t st = ws_client_poll(s_cws, now);
            pthread_mutex_lock(&G.mu);
            bool finished = !G.chat.streaming;
            if(st == WS_CLOSED && chat_live && G.chat.streaming) {
                chat_finish_locked(false, ws_client_error(s_cws)[0] ? ws_client_error(s_cws) : "connection lost");
                finished = true;
            }
            pthread_mutex_unlock(&G.mu);
            if(finished && chat_live) {
                ws_client_destroy(s_cws);
                s_cws = NULL;
                chat_live = false;
            }
        }
        usleep(s_vws && G.vis.open ? 2000 : 8000);
    }
    ws_client_destroy(sys);
    ws_client_destroy(s_vws);
    ws_client_destroy(s_cws);
    s_vws = s_cws = NULL;
    return NULL;
}

/* ---------- http thread ---------- */

/* GET over a plain socket, Connection: close. Returns a malloc'd body or NULL. Handles chunked bodies. */
static char *http_get(const char *path, size_t *out_len)
{
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM }, *ai = NULL;
    char port[8];
    snprintf(port, sizeof port, "%d", G.port);
    if(getaddrinfo(G.host, port, &hints, &ai) != 0 || !ai) return NULL;
    int fd = socket(ai->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if(fd < 0) { freeaddrinfo(ai); return NULL; }
    int r = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    struct pollfd p = { fd, POLLOUT, 0 };
    if(r != 0 && (errno != EINPROGRESS || poll(&p, 1, 2000) <= 0)) { close(fd); return NULL; }
    int err = 0;
    socklen_t l = sizeof err;
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &l);
    if(err) { close(fd); return NULL; }
    char full[256], req[512];
    with_key(path, full, sizeof full);
    int n = snprintf(req, sizeof req, "GET %s HTTP/1.1\r\nHost: %s:%d\r\nAccept: application/json\r\nConnection: close\r\n\r\n", full, G.host, G.port);
    if(send(fd, req, (size_t)n, MSG_NOSIGNAL) != n) { close(fd); return NULL; }
    size_t cap = 16384, len = 0;
    char *buf = malloc(cap);
    for(;;) {
        if(len + 4096 > cap) { cap *= 2; buf = realloc(buf, cap); }
        struct pollfd pr = { fd, POLLIN, 0 };
        if(poll(&pr, 1, 3000) <= 0) break;
        ssize_t k = recv(fd, buf + len, cap - len - 1, 0);
        if(k <= 0) break;
        len += (size_t)k;
        if(len > (8u << 20)) break;
    }
    close(fd);
    buf[len] = '\0';
    char *hdr_end = strstr(buf, "\r\n\r\n");
    if(!hdr_end || strncmp(buf, "HTTP/1.", 7) != 0 || strncmp(buf + 9, "200", 3) != 0) { free(buf); return NULL; }
    *hdr_end = '\0';
    bool chunked = strcasestr(buf, "transfer-encoding: chunked") != NULL;
    char *body = hdr_end + 4;
    size_t blen = len - (size_t)(body - buf);
    char *out = malloc(blen + 1);
    if(!chunked) {
        memcpy(out, body, blen);
        out[blen] = '\0';
        *out_len = blen;
    }
    else {
        size_t o = 0;
        char *q = body;
        while(q < body + blen) {
            char *e;
            unsigned long cl = strtoul(q, &e, 16);
            char *nl = strstr(e, "\r\n");
            if(!nl || cl == 0) break;
            q = nl + 2;
            if(q + cl > body + blen) cl = (size_t)(body + blen - q);
            memcpy(out + o, q, cl);
            o += cl;
            q += cl + 2;
        }
        out[o] = '\0';
        *out_len = o;
    }
    free(buf);
    return out;
}

static void fetch_models(void)
{
    size_t len;
    char *body = http_get("/v1/models", &len);
    if(!body) {
        pthread_mutex_lock(&G.mu);
        G.snap.models_ok = false;
        pthread_mutex_unlock(&G.mu);
        return;
    }
    cbj_t *root = cbj_parse(body, len);
    free(body);
    if(!root) return;
    const cbj_t *data = cbj_get(root, "data");
    pthread_mutex_lock(&G.mu);
    net_snapshot_t *s = &G.snap;
    s->n_models = 0;
    for(int i = 0; data && i < data->count && s->n_models < NET_MAX_MODELS; i++) {
        const cbj_t *m = cbj_at(data, i);
        net_model_t *d = &s->models[s->n_models++];
        cpy(d->id, sizeof d->id, cbj_str(cbj_get(m, "id"), "?"));
        cpy(d->modality, sizeof d->modality, cbj_str(cbj_get(m, "modality"), "-"));
        cpy(d->runtime, sizeof d->runtime, cbj_str(cbj_get(m, "runtime"), "-"));
        cpy(d->device, sizeof d->device, cbj_str(cbj_get(m, "device"), "-"));
        cpy(d->state, sizeof d->state, cbj_str(cbj_get(m, "state"), cbj_bool(cbj_get(m, "resident"), false) ? "resident" : "unloaded"));
        cpy(d->err, sizeof d->err, cbj_str(cbj_get(m, "error"), ""));
        d->resident = cbj_bool(cbj_get(m, "resident"), false);
    }
    s->models_ok = true;
    s->models_seq++;
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
}

static void fetch_videos(void)
{
    size_t len;
    char *body = http_get("/v1/vision/videos", &len);
    cbj_t *root = body ? cbj_parse(body, len) : NULL;
    free(body);
    pthread_mutex_lock(&G.mu);
    net_snapshot_t *s = &G.snap;
    const cbj_t *data = root ? cbj_get(root, "data") : NULL;
    int old = s->n_videos;
    s->n_videos = 0;
    for(int i = 0; data && i < data->count && s->n_videos < NET_MAX_VIDEOS; i++) {
        const cbj_t *m = cbj_at(data, i);
        net_video_t *d = &s->videos[s->n_videos++];
        cpy(d->id, sizeof d->id, cbj_str(cbj_get(m, "id"), "?"));
        cpy(d->name, sizeof d->name, cbj_str(cbj_get(m, "name"), d->id));
        d->duration_s = num(m, "duration_s", 0);
    }
    if(old != s->n_videos || s->videos_seq == 0) s->videos_seq++;
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
}

static char *http_face(const char *method, const char *path, const char *body, int *status)
{
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM }, *ai = NULL;
    char port[8];
    snprintf(port, sizeof port, "%d", G.port);
    if(getaddrinfo(G.host, port, &hints, &ai) != 0 || !ai) return NULL;
    int fd = socket(ai->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if(fd < 0) { freeaddrinfo(ai); return NULL; }
    int r = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    struct pollfd p = { fd, POLLOUT, 0 };
    if(r != 0 && (errno != EINPROGRESS || poll(&p, 1, 2000) <= 0)) { close(fd); return NULL; }
    int err = 0;
    socklen_t l = sizeof err;
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &l);
    if(err) { close(fd); return NULL; }
    char full[256], req[1024];
    with_key(path, full, sizeof full);
    size_t body_len = body ? strlen(body) : 0;
    int n = snprintf(req, sizeof req,
        "%s %s HTTP/1.1\r\nHost: %s:%d\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s",
        method, full, G.host, G.port, body_len, body ? body : "");
    if(send(fd, req, (size_t)n, MSG_NOSIGNAL) != n) { close(fd); return NULL; }
    size_t cap = 16384, len = 0;
    char *buf = malloc(cap);
    if(!buf) { close(fd); return NULL; }
    for(;;) {
        if(len + 4096 > cap) {
            cap *= 2;
            char *next = realloc(buf, cap);
            if(!next) { free(buf); close(fd); return NULL; }
            buf = next;
        }
        struct pollfd pr = { fd, POLLIN, 0 };
        if(poll(&pr, 1, 60000) <= 0) break;
        ssize_t k = recv(fd, buf + len, cap - len - 1, 0);
        if(k <= 0) break;
        len += (size_t)k;
        if(len > (8u << 20)) break;
    }
    close(fd);
    buf[len] = '\0';
    char *hdr_end = strstr(buf, "\r\n\r\n");
    if(!hdr_end || strncmp(buf, "HTTP/1.", 7) != 0) { free(buf); return NULL; }
    *status = atoi(buf + 9);
    char *out = strdup(hdr_end + 4);
    free(buf);
    return out;
}

static void fetch_face_users(void)
{
    size_t len = 0;
    char *body = http_get("/v1/face/users", &len);
    cbj_t *root = body ? cbj_parse(body, len) : NULL;
    free(body);
    if(!root) return;
    const cbj_t *users = cbj_get(root, "users");
    pthread_mutex_lock(&G.mu);
    G.face.n_users = 0;
    for(int i = 0; users && i < users->count && G.face.n_users < NET_MAX_FACE_USERS; i++) {
        const cbj_t *src = cbj_at(users, i);
        net_face_user_t *dst = &G.face.users[G.face.n_users++];
        memset(dst, 0, sizeof *dst);
        cpy(dst->user_id, sizeof dst->user_id, cbj_str(cbj_get(src, "user_id"), ""));
        cpy(dst->name, sizeof dst->name, cbj_str(cbj_get(src, "name"), dst->user_id));
        cpy(dst->role, sizeof dst->role, cbj_str(cbj_get(src, "role"), "unassigned"));
        dst->on_sensor = cbj_bool(cbj_get(src, "on_sensor"), true);
    }
    G.face.users_gen++;
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
}

static void json_quote(const char *src, char *out, size_t cap)
{
    size_t n = 0;
    if(cap < 3) return;
    out[n++] = '"';
    for(const unsigned char *p = (const unsigned char *)src; *p && n + 7 < cap; p++) {
        if(*p == '"' || *p == '\\') { out[n++] = '\\'; out[n++] = (char)*p; }
        else if(*p == '\n') { out[n++] = '\\'; out[n++] = 'n'; }
        else if(*p == '\r') { out[n++] = '\\'; out[n++] = 'r'; }
        else if(*p == '\t') { out[n++] = '\\'; out[n++] = 't'; }
        else if(*p >= 0x20) out[n++] = (char)*p;
    }
    out[n++] = '"';
    out[n] = '\0';
}

static void fetch_face(int op, const char *name, const char *role, const char *user_id, bool confirm)
{
    int status = 0;
    char *body = NULL;
    char path[96], payload[256];
    if(op == 1) body = http_face("POST", "/v1/face/authenticate", "{}", &status);
    else if(op == 2) {
        char qname[160], qrole[32];
        json_quote(name, qname, sizeof qname);
        json_quote(role, qrole, sizeof qrole);
        snprintf(payload, sizeof payload, "{\"name\":%s,\"role\":%s%s}", qname, qrole,
                 confirm ? ",\"confirm\":true" : "");
        body = http_face("POST", "/v1/face/enroll", payload, &status);
    }
    else if(op == 3) {
        snprintf(path, sizeof path, "/v1/face/users/%s", user_id);
        body = http_face("DELETE", path, NULL, &status);
    }
    else if(op == 4) body = http_face("DELETE", "/v1/face/users", NULL, &status);

    cbj_t *root = body ? cbj_parse(body, strlen(body)) : NULL;
    free(body);
    pthread_mutex_lock(&G.mu);
    G.face.busy = false;
    G.face.done = op != 5;
    if(op != 5 && (!root || status < 200 || status >= 300)) {
        const cbj_t *error = root ? cbj_get(root, "error") : NULL;
        const char *message = error ? cbj_str(cbj_get(error, "message"), "Face ID request failed") : "Face ID request failed";
        cpy(G.face.message, sizeof G.face.message, message);
        G.face.driver_exists = error && !strcmp(cbj_str(cbj_get(error, "code"), ""), "driver_exists");
        const cbj_t *existing = root ? cbj_get(root, "existing") : NULL;
        cpy(G.face.existing_name, sizeof G.face.existing_name, cbj_str(cbj_get(existing, "name"), "current driver"));
    }
    else if(root && op != 5) {
        cpy(G.face.status, sizeof G.face.status, cbj_str(cbj_get(root, "status"), "Success"));
        cpy(G.face.user, sizeof G.face.user, cbj_str(cbj_get(root, "user_id"), ""));
        cpy(G.face.name, sizeof G.face.name, cbj_str(cbj_get(root, "name"), ""));
        cpy(G.face.role, sizeof G.face.role, cbj_str(cbj_get(root, "role"), ""));
        cpy(G.face.message, sizeof G.face.message, cbj_str(cbj_get(root, "message"), ""));
        cpy(G.face.security, sizeof G.face.security, cbj_str(cbj_get(root, "security_level"), ""));
        cpy(G.face.flow, sizeof G.face.flow, cbj_str(cbj_get(root, "algo_flow"), ""));
        G.face.authenticated = cbj_bool(cbj_get(root, "authenticated"), false);
        if(op == 1) G.face.ok = G.face.authenticated;
        else if(op == 2) G.face.ok = cbj_bool(cbj_get(root, "enrolled"), false);
        else G.face.ok = cbj_bool(cbj_get(root, "deleted"), false);
        if(!G.face.message[0]) cpy(G.face.message, sizeof G.face.message, G.face.ok ? "Success" : "Face ID operation failed");
    }
    G.face.gen++;
    pthread_mutex_unlock(&G.mu);
    cbj_free(root);
    if(op >= 2 && op <= 4) fetch_face_users();
}

/* the Studio's own polling and the health probe are noise in the request list */
static bool is_noise(const cbj_t *o)
{
    const char *ep = cbj_str(cbj_get(o, "endpoint"), "");
    return !strncmp(ep, "/v1/inspector", 13) || !strcmp(ep, "/v1/models") || !strncmp(ep, "/v1/system", 10) || !strcmp(ep, "/health") ||
           !strcmp(ep, "/v1/clients");
}

static void fetch_recent(void)
{
    size_t len;
    char *body = http_get("/v1/inspector/recent?n=400", &len);
    if(!body) return;
    cbj_t *root = cbj_parse(body, len);
    free(body);
    if(!root) return;
    const cbj_t *arr = root;
    if(root->type == CBJ_OBJ) {
        static const char *const keys[] = { "data", "rows", "items", "entries", "events", "recent" };
        arr = NULL;
        for(size_t i = 0; i < sizeof keys / sizeof *keys && !arr; i++) {
            const cbj_t *c = cbj_get(root, keys[i]);
            if(c && c->type == CBJ_ARR) arr = c;
        }
    }
    if(arr && arr->type == CBJ_ARR) {
        net_row_t rows[NET_MAX_ROWS];
        int n = 0;
        /* oldest-first in the feed; keep the newest NET_MAX_ROWS requests, newest first. Events go to the log. */
        for(int i = arr->count - 1; i >= 0; i--) {
            const cbj_t *o = cbj_at(arr, i);
            if(!strcmp(cbj_str(cbj_get(o, "kind"), ""), "event") || (cbj_get(o, "event") && !cbj_get(o, "endpoint")) || is_noise(o)) continue;
            if(n < NET_MAX_ROWS) parse_row(o, &rows[n++]);
        }
        /* feeds may arrive newest-first: order by the formatted time is not reliable, so sort by t descending */
        double ts[NET_MAX_ROWS];
        int idx = 0;
        for(int i = arr->count - 1; i >= 0 && idx < n; i--) {
            const cbj_t *o = cbj_at(arr, i);
            if(!strcmp(cbj_str(cbj_get(o, "kind"), ""), "event") || (cbj_get(o, "event") && !cbj_get(o, "endpoint")) || is_noise(o)) continue;
            ts[idx++] = num(o, "t", 0);
        }
        for(int i = 1; i < n; i++)
            for(int j = i; j > 0 && ts[j] > ts[j - 1]; j--) {
                double td = ts[j]; ts[j] = ts[j - 1]; ts[j - 1] = td;
                net_row_t tr = rows[j]; rows[j] = rows[j - 1]; rows[j - 1] = tr;
            }
        pthread_mutex_lock(&G.mu);
        memcpy(G.snap.rows, rows, (size_t)n * sizeof rows[0]);
        G.snap.n_rows = n;
        G.snap.rows_seq++;
        pthread_mutex_unlock(&G.mu);
        for(int i = 0; i < arr->count; i++) {
            const cbj_t *o = cbj_at(arr, i);
            if(!strcmp(cbj_str(cbj_get(o, "kind"), ""), "event") || (cbj_get(o, "event") && !cbj_get(o, "endpoint"))) on_event_json(o);
        }
    }
    cbj_free(root);
}

static void *http_main(void *arg)
{
    (void)arg;
    uint32_t last_models = 0, last_recent = 0;
    bool first = true;
    while(!G.stop) {
        uint32_t now = now_ms();
        if(first || now - last_models >= 5000) { fetch_models(); fetch_videos(); last_models = now; }
        if(first || now - last_recent >= 2000) { fetch_recent(); last_recent = now; }
        pthread_mutex_lock(&G.mu);
        int face_op = G.f_op;
        char face_name[32], face_role[12], face_user_id[32];
        cpy(face_name, sizeof face_name, G.f_name);
        cpy(face_role, sizeof face_role, G.f_role);
        cpy(face_user_id, sizeof face_user_id, G.f_user_id);
        bool face_confirm = G.f_confirm;
        if(face_op) G.f_op = 0;
        pthread_mutex_unlock(&G.mu);
        if(face_op == 5) fetch_face_users();
        else if(face_op) fetch_face(face_op, face_name, face_role, face_user_id, face_confirm);
        first = false;
        for(int i = 0; i < 10 && !G.stop; i++) usleep(50000);
    }
    return NULL;
}

bool net_start(const char *base_url, const char *key)
{
    const char *u = base_url;
    if(!strncmp(u, "http://", 7)) u += 7;
    const char *colon = strchr(u, ':'), *slash = strchr(u, '/');
    size_t hl = colon ? (size_t)(colon - u) : (slash ? (size_t)(slash - u) : strlen(u));
    if(hl == 0 || hl >= sizeof G.host) return false;
    memcpy(G.host, u, hl);
    G.port = colon ? atoi(colon + 1) : 80;
    if(G.port <= 0) return false;
    snprintf(G.base, sizeof G.base, "http://%s:%d", G.host, G.port);
    cpy(G.key, sizeof G.key, key);
    for(int i = 0; i < 3; i++) {
        G.vbuf[i] = calloc(1, PREVIEW_MAX_BYTES);
        if(!G.vbuf[i]) return false;
    }
    G.v_back = 0;
    G.v_ready = 1;
    G.v_front = 2;
    G.started = true;
    pthread_create(&G.th_net, NULL, net_main, NULL);
    pthread_create(&G.th_http, NULL, http_main, NULL);
    return true;
}

void net_stop(void)
{
    if(!G.started) return;
    G.stop = true;
    pthread_join(G.th_net, NULL);
    pthread_join(G.th_http, NULL);
    G.started = false;
}

const char *net_base_url(void) { return G.base; }
