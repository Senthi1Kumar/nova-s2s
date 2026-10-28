#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Nova bar state machine. Pure C (no LVGL): events in, effect bits out; the caller supplies the clock.
   Mirrors nova-hailo's WakeGate contract: nothing is heard while IDLE; a wake opens a listening window;
   after Nova answers, a follow-up window sized by the dialogue state measures silence, then IDLE and,
   after a short settle, the bar rests. */

typedef enum { NV_IDLE, NV_LISTENING, NV_THINKING, NV_SPEAKING } nv_activity_t;
typedef enum { NV_VIEW_OPEN, NV_VIEW_REST } nv_view_t;
typedef enum { NV_OUT_DONE, NV_OUT_ERROR } nv_outcome_t;

typedef enum {
    NV_EV_WAKE_KWS, NV_EV_WAKE_TAP, NV_EV_WAKE_PTT,
    NV_EV_USER_SPEAKING_ON, NV_EV_USER_SPEAKING_OFF, NV_EV_TURN_COMMITTED,
    NV_EV_AGENT_THINKING, NV_EV_AGENT_SPEAKING, NV_EV_AGENT_DONE,
    NV_EV_TAP_ORB, NV_EV_TAP_MIC, NV_EV_TAP_END, NV_EV_TAP_TUCK, NV_EV_TICK
} nv_event_t;

/* Effect bits returned by nova_fsm_handle(). The REQ bits appear only in remote mode: the pipeline owns the mic,
   so a tap becomes a request (mute toggles to !muted) and the picture changes when the server's answer arrives. */
enum {
    NV_FX_VIEW = 1u, NV_FX_ACTIVITY = 2u, NV_FX_ABORT = 4u, NV_FX_CHIME = 8u, NV_FX_MUTE = 16u,
    NV_FX_REQ_MUTE = 32u, NV_FX_REQ_ARM = 64u, NV_FX_REQ_CANCEL = 128u
};

typedef struct { bool expects_answer; nv_outcome_t outcome; } nv_done_t;
typedef struct { uint32_t answer_ms, error_ms, done_ms, settle_ms, wake_ms; } nv_followup_cfg_t;
#define NV_FOLLOWUP_DEFAULTS ((nv_followup_cfg_t){ 8000u, 4000u, 2000u, 700u, 8000u })

typedef struct {
    nv_activity_t activity;
    nv_view_t view;
    bool muted;
    bool user_speaking;
    bool window_armed;          /* follow-up / wake silence window running */
    uint32_t window_ms;
    uint32_t window_deadline;
    bool settling;              /* IDLE after a window expired; the view rests at settle_deadline */
    uint32_t settle_deadline;
    nv_followup_cfg_t cfg;
    bool remote;                /* the pipeline drives the state; mic / orb / end taps become REQ bits */
} nova_fsm_t;

void nova_fsm_init(nova_fsm_t *f, const nv_followup_cfg_t *cfg);   /* NULL -> defaults; starts IDLE + REST, unmuted */
unsigned nova_fsm_handle(nova_fsm_t *f, nv_event_t ev, const nv_done_t *done, uint32_t now_ms);
uint32_t nova_followup_ms(const nv_followup_cfg_t *cfg, const nv_done_t *done);
/* Live mode: the pipeline's FSM state (IDLE ARMED LISTENING TRANSCRIBING THINKING SPEAKING INTERRUPTING) drives
   the bar; the view opens on any non-idle state and rests after the settle delay once IDLE. */
unsigned nova_fsm_remote(nova_fsm_t *f, const char *state, uint32_t now_ms);
/* Live mode: the pipeline's mic state (nova.mic). Mute only closes the mic, so the activity is left alone. */
unsigned nova_fsm_remote_mute(nova_fsm_t *f, bool muted);
const char *nova_activity_name(nv_activity_t a);                    /* "IDLE" "LISTENING" "THINKING" "SPEAKING" */
