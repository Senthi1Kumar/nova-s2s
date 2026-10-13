#pragma once
#include "nova/nova_fsm.h"
#include <stdbool.h>
#include <stddef.h>

/* Scripted conversation runner (pure C, no LVGL). Steps are data; the runner feeds events to the FSM,
   text to the bar and effects to the model through `cb_demo_io_t`, honouring each step's delay. */

typedef enum { DS_EVENT, DS_USER, DS_NOVA, DS_APPLY } cb_step_kind_t;

typedef struct {
    cb_step_kind_t kind;
    uint32_t after_ms;      /* wait after executing this step */
    nv_event_t ev;          /* DS_EVENT */
    nv_done_t done;         /* DS_EVENT with NV_EV_AGENT_DONE */
    const char *text;       /* DS_USER / DS_NOVA */
    int apply_id;           /* DS_APPLY */
    bool turn_start;        /* an abort resumes at the next step with this set */
    bool always;            /* housekeeping: runs even when no conversation is live */
} cb_step_t;

typedef struct {
    void *ctx;
    unsigned (*emit)(void *ctx, nv_event_t ev, const nv_done_t *done);   /* returns the FSM effect bits */
    void (*say)(void *ctx, const char *text, bool user);
    void (*apply)(void *ctx, int apply_id);
    bool (*live)(void *ctx);   /* is a conversation active? NULL = always. Not live: the turn is skipped */
} cb_demo_io_t;

typedef struct {
    const cb_step_t *steps;
    size_t n;
    size_t i;               /* index of the next step to execute */
    uint32_t next_at;
    uint32_t run_id;        /* bumped on every abort */
    bool paused;
    cb_demo_io_t io;
} cb_demo_t;

void cb_demo_init(cb_demo_t *d, const cb_step_t *steps, size_t n, cb_demo_io_t io, uint32_t now_ms,
                  uint32_t first_delay_ms);
void cb_demo_tick(cb_demo_t *d, uint32_t now_ms);    /* runs every due step */
void cb_demo_abort(cb_demo_t *d, uint32_t now_ms);   /* run_id++, jump to the next turn_start, resume after 1500 ms */
void cb_demo_set_paused(cb_demo_t *d, bool paused);

/* The M1 script (spec §9) and its model effects, in cb_demo_script.c. */
extern const cb_step_t cb_demo_script[];
extern const size_t cb_demo_script_len;
enum {
    APPLY_WARMER_CALL_SAM = 1, APPLY_CRACK_REAR, APPLY_FOG_DECLINE, APPLY_FIND_COFFEE, APPLY_NAV_CAFE,
    APPLY_SUNROOF_ASK, APPLY_SUNROOF_OPEN, APPLY_RESET
};
void cb_demo_apply_to_model(int apply_id);   /* needs cb_model_init() */
