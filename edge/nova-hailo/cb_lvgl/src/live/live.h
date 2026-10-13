#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* CabinEnv live data: frames from nova-hailo's /v1/observe (or a recorded JSONL replayed at its own pace) drive
   the cabin model, the Nova bar, the Last-turn card, answer cards and the map; touch writes go back through
   POST /v1/vehicle/set and come home as ordinary vehicle deltas. Single instance, polled by an LVGL timer. */
bool live_start(const char *url);                                     /* ws://host:port/v1/observe */
bool live_start_replay(const char *path, double speed, bool loop);    /* {"t_ms":..,"frame":{..}} per line */
bool live_active(void);
void live_feed(const char *json, size_t len);                         /* one frame (the timer and tests call it) */
uint32_t live_seq(void);
uint32_t live_seq_gaps(void);
int live_polyline6(const char *s, double (*lonlat)[2], int cap);      /* points decoded, [lon, lat] */
size_t live_set_request(char *out, size_t cap, const char *host, int port, const char *vss_path, double value);
void live_temp_step(bool driver, int32_t delta_dc);                   /* dock -/+ while live */
/* Map chip text: GpsFix decides when the stream has it (-1 = unknown), else the snapshot's position source;
   while a route plays the simulated drive owns the position even under a fix. True when the puck is GPS. */
bool live_position_label(int gps_fix, int sats, const char *source, bool driving, char *out, size_t cap);

/* Nova's language model, as GET/POST /v1/settings and the nova.settings observe frame describe it. */
#define LIVE_LLM_PROVIDERS 4
#define LIVE_LLM_MODELS 8
typedef struct {
    char provider[24], model[96];
    bool failover;         /* the switch (saved on the Pi) */
    bool failover_ready;   /* a second provider stands by right now */
    int n;
    struct {
        char id[24];
        bool ok;
        int n_models;
        char models[LIVE_LLM_MODELS][96];
    } p[LIVE_LLM_PROVIDERS];
} live_llm_t;

bool live_llm_parse_json(const char *json, size_t len, live_llm_t *out);   /* {"llm":{...}} */
/* model NULL: the body has no "model" and the pipeline keeps the one it runs */
size_t live_llm_request(char *out, size_t cap, const char *host, int port, const char *provider, const char *model,
                        bool failover);
bool live_llm_fetch(live_llm_t *out, char *err, size_t err_cap);   /* needs a live link */
/* Blocks up to 20 s (a switch to the on-device model loads it first): call it off the UI thread. */
bool live_llm_apply(const char *provider, const char *model, bool failover, live_llm_t *now, char *err, size_t err_cap);
void live_on_llm(void (*cb)(const live_llm_t *llm));              /* nova.settings frames */
bool live_linked(void);                                           /* a pipeline is reachable over --live */

/* Mic and turn control (POST /v1/session/{mute,arm,cancel}): the pipeline owns the mic, the cockpit only asks, and
   the nova.mic / nova.fsm frames that follow change the picture. */
typedef enum { LIVE_SESSION_MUTE, LIVE_SESSION_ARM, LIVE_SESSION_CANCEL } live_session_op_t;
size_t live_session_request(char *out, size_t cap, const char *host, int port, live_session_op_t op, bool muted);
/* Blocks up to ~1.5 s: call it off the UI thread. *muted_now (may be NULL) gets the server's mic state. */
bool live_session_post(live_session_op_t op, bool muted, bool *muted_now, char *err, size_t err_cap);
void live_toast(const char *title, const char *detail);           /* the cockpit's transient toast (UI thread) */
