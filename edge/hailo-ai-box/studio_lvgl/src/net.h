#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/* All network I/O lives on two worker threads (net: WebSockets, http: plain GETs). They write into one
   mutex-guarded model; the LVGL thread only ever calls the *_snapshot / *_take functions below, which copy
   out under the lock. */

#define NET_MAX_MODELS 32
#define NET_MAX_MSTAT 24
#define NET_MAX_EVENTS 40
#define NET_MAX_ROWS 20
#define NET_MAX_CORES 8
#define NET_MAX_BOXES 48
#define NET_MAX_LANES 4
#define NET_LANE_PTS 24
#define NET_MAX_KP 192
#define NET_CHAT_CAP 6144
#define NET_MAX_VIDEOS 12

typedef struct {
    char id[40], modality[8], state[12], runtime[20], device[8], err[96];
    bool resident;
} net_model_t;

typedef struct {
    char id[40], state[12];
    double fps, p50, p95;
    long requests;
} net_mstat_t;

typedef struct {
    char t[12], client[24], endpoint[32], model[24], device[8];
    double queue_ms, infer_ms;
    int status;
} net_row_t;

typedef struct {
    char id[40], name[40];
    double duration_s;
} net_video_t;

typedef struct {
    bool online;          /* system stream open */
    char err[96];
    uint32_t seq;         /* ++ per metrics snapshot */
    double cpu_pct, core[NET_MAX_CORES];
    int ncores;
    double mem_used, mem_total, temp_c, arm_mhz, load1;
    char throttled[16];
    double npu_util, npu_ram_used, npu_ram_total, npu_temp;
    char genai_owner[40], genai_state[16];
    net_mstat_t ms[NET_MAX_MSTAT];
    int n_ms;
    double rps, req_total, uptime;
    int clients;
    net_model_t models[NET_MAX_MODELS];
    int n_models;
    bool models_ok;
    uint32_t models_seq;
    char events[NET_MAX_EVENTS][120];   /* newest first */
    int n_events;
    uint32_t ev_seq;
    net_row_t rows[NET_MAX_ROWS];       /* newest first */
    int n_rows;
    uint32_t rows_seq;
    net_video_t videos[NET_MAX_VIDEOS];   /* GET /v1/vision/videos; empty when the endpoint is missing */
    int n_videos;
    uint32_t videos_seq;
} net_snapshot_t;

typedef struct {
    float x1, y1, x2, y2;
    char label[32];
} net_box_t;

typedef struct {
    int n;
    float x[NET_LANE_PTS], y[NET_LANE_PTS];
} net_poly_t;

#define NET_MAX_MASKS 4
#define NET_MASK_RUNS 8192

typedef struct {
    int w, h, box, n_runs;
    uint32_t counts[NET_MASK_RUNS];  /* COCO RLE, column-major, in the source image coordinates */
} net_mask_t;

typedef struct {
    bool active, open;           /* stream requested / connected */
    char err[96];
    uint32_t pgen, rgen;         /* preview frames / result messages received */
    const uint16_t *pix;         /* RGB565 front buffer, valid until the next net_vision_take */
    int w, h;                    /* preview size */
    int img_w, img_h;            /* coordinate space of the overlays */
    net_box_t boxes[NET_MAX_BOXES];
    int n_boxes, n_det;
    net_mask_t masks[NET_MAX_MASKS];
    int n_masks;
    net_poly_t lanes[NET_MAX_LANES];
    int n_lanes;
    float kp[NET_MAX_KP][2];
    int n_kp;
    double fps, infer_ms, e2e_ms;
    char task[16], info[120], degraded[24], engine[24];
    char loading[40];            /* model being loaded: server sent {"type":"status","state":"loading"} */
    double video_t;              /* seconds into a video source, < 0 for cameras */
    char dms_status[20], dms_summary[120];   /* dms.status / dms.summary, empty outside Driver Monitoring */
    float face_lm[5][2];         /* optional SCRFD landmarks, in img_w/img_h coordinates */
    int n_face_lm;
    bool face_held;
} net_vision_t;

typedef struct {
    bool streaming, done, error;
    uint32_t gen;                /* ++ on every change */
    char text[NET_CHAT_CAP];
    char err[160], note[80];
    double ttft_ms, tps;
    int tokens;
} net_chat_t;

/* base like "http://127.0.0.1:7442"; key may be NULL. Starts both threads. */
bool net_start(const char *base_url, const char *key);
void net_stop(void);
const char *net_base_url(void);
void net_ws_url(const char *path, char *out, size_t cap);   /* ws://host:port<path>[?key=...] */

void net_snapshot(net_snapshot_t *out);   /* only copies when something changed is the caller's job: compare the seq fields */

/* source is a camera id ("c920e") or "video:<id>" for a preset/uploaded video. */
void net_vision_start(const char *source, const char *task, int fps, int preview_w);
void net_vision_stop(void);
void net_vision_take(net_vision_t *out);

/* roles[i] is "user" or "assistant". image_path optionally attaches a JPEG/PNG to the last user turn. */
bool net_chat_send(const char *model, int n, const char *const *roles, const char *const *texts, int max_tokens, const char *image_path);
void net_chat_cancel(void);
void net_chat_take(net_chat_t *out);

#define NET_MAX_FACE_USERS 16

typedef struct {
    char user_id[32], name[32], role[12];
    bool on_sensor;
} net_face_user_t;

typedef struct {
    bool busy, done, ok, authenticated, driver_exists;
    uint32_t gen, users_gen;
    char status[48], user[40], name[32], role[12], message[192];
    char existing_name[32], security[24], flow[24];
    net_face_user_t users[NET_MAX_FACE_USERS];
    int n_users;
} net_face_t;

bool net_face_authenticate(void);
bool net_face_enroll(const char *name, const char *role, bool confirm);
bool net_face_delete(const char *user_id);   /* empty user_id deletes every user */
bool net_face_refresh(void);
void net_face_take(net_face_t *out);
