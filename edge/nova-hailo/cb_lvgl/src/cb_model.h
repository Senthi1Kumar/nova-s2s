#pragma once
#include "lvgl.h"
#include <stdbool.h>

/* The simulated cabin. One instance; every observable field is an LVGL subject so widgets update by
   observing, never by polling. Setters clamp and skip equal values (no redundant redraws). In M4 the
   same subjects are fed by the live pipeline / CabinGym instead of cb_sim and cb_demo. */

typedef enum { CB_GEAR_P, CB_GEAR_R, CB_GEAR_N, CB_GEAR_D } cb_gear_t;
typedef enum { CB_WIN_FL, CB_WIN_FR, CB_WIN_RL, CB_WIN_RR, CB_WIN_COUNT } cb_window_t;
typedef enum { CB_VERDICT_EXECUTE, CB_VERDICT_CONFIRM, CB_VERDICT_DECLINE } cb_verdict_t;

typedef struct { const char *api; const char *args; cb_verdict_t verdict; } cb_call_t;
typedef struct { const char *path; const char *change; } cb_diff_t;
typedef struct {
    const char *utterance;
    const char *router;
    const char *latency;
    cb_call_t calls[3];
    int n_calls;
    cb_diff_t diffs[3];
    int n_diffs;
} cb_last_turn_t;

/* Subjects are created with lv_subject_create() (v9.6 deprecates in-place init), hence pointers. */
typedef struct {
    lv_subject_t *speed_kph, *gear, *soc_pct, *range_km, *route_permille, *eta_min;
    lv_subject_t *driver_temp_dc, *passenger_temp_dc, *fan_level, *ac_on;
    lv_subject_t *win_pct[CB_WIN_COUNT], *sunroof_pct, *wipers_on, *locked, *seat_heat;
    lv_subject_t *sunshade_pct;    /* 0 retracted (open) .. 100 deployed (closed), as VSS */
    lv_subject_t *wiper_mode;      /* 0 off, 1 auto, 2 low, 3 high */
    lv_subject_t *headlights;      /* 0 off, 1 auto, 2 low, 3 high */
    lv_subject_t *defrost;         /* bit 0 front, bit 1 rear */
    lv_subject_t *mirrors_folded;  /* 0 unfolded, 1 folded */
    lv_subject_t *drive_mode;      /* 0 eco, 1 comfort, 2 sport */
    lv_subject_t *ambient_color;   /* 0 white, 1 blue, 2 red, 3 green, 4 purple, 5 orange, 6 pink */
    lv_subject_t *ambient_level;   /* 0..100 */
    lv_subject_t *media_playing, *media_track, *media_pos_s, *volume, *media_title_rev;
    lv_subject_t *call_active, *call_s, *session_s;
    lv_subject_t *last_turn;   /* pointer subject -> const cb_last_turn_t * */
} cb_model_t;

cb_model_t *cb_model(void);
void cb_model_init(void);                              /* requires lv_init(); idempotent */
void cb_model_set_int(lv_subject_t *s, int32_t v);     /* no-op when equal */
void cb_model_set_speed(int32_t kph);                  /* clamp 0..250 */
void cb_model_set_soc(int32_t pct);                    /* clamp 0..100; range_km = pct * 309 / 100 */
void cb_model_set_temp(bool driver, int32_t deci_c);   /* clamp 160..300 */
void cb_model_set_window(cb_window_t w, int32_t pct);  /* clamp 0..100 */
void cb_model_set_sunroof(int32_t pct);                /* clamp -100..100; below 0 is tilted, as VSS */
void cb_model_set_last_turn(const cb_last_turn_t *t);
const char *cb_model_track_title(int32_t idx);         /* 3 tracks; idx wraps */
int32_t cb_model_track_duration(int32_t idx);          /* seconds */
void        cb_model_set_media_title(const char *title);
const char *cb_model_media_title(void);
