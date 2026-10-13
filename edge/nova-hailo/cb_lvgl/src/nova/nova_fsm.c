#include "nova/nova_fsm.h"
#include <stddef.h>
#include <string.h>

/* Wrap-safe "now has reached deadline" for a uint32 millisecond clock. */
static bool due(uint32_t now, uint32_t deadline) { return (int32_t)(now - deadline) >= 0; }

static void arm(nova_fsm_t *f, uint32_t ms, uint32_t now)
{
    f->window_armed = true;
    f->window_ms = ms;
    f->window_deadline = now + ms;
}

static void disarm(nova_fsm_t *f) { f->window_armed = false; }

static void set_activity(nova_fsm_t *f, nv_activity_t a, unsigned *fx)
{
    if(f->activity == a) return;
    f->activity = a;
    *fx |= NV_FX_ACTIVITY;
}

static void set_view(nova_fsm_t *f, nv_view_t v, unsigned *fx)
{
    if(f->view == v) return;
    f->view = v;
    *fx |= NV_FX_VIEW;
}

static bool busy(const nova_fsm_t *f) { return f->activity == NV_THINKING || f->activity == NV_SPEAKING; }

/* Shared by the wake word, push-to-talk, a remote tap and an unmuted orb tap. */
static unsigned wake(nova_fsm_t *f, uint32_t now)
{
    unsigned fx = 0;
    f->settling = false;
    set_view(f, NV_VIEW_OPEN, &fx);
    if(busy(f)) fx |= NV_FX_ABORT;                 /* barge-in: Nova stops mid-answer */
    if(f->activity == NV_IDLE) fx |= NV_FX_CHIME;  /* chime only on the idle -> listening edge */
    set_activity(f, NV_LISTENING, &fx);
    arm(f, f->cfg.wake_ms, now);
    return fx;
}

void nova_fsm_init(nova_fsm_t *f, const nv_followup_cfg_t *cfg)
{
    *f = (nova_fsm_t){ .activity = NV_IDLE, .view = NV_VIEW_REST, .cfg = cfg ? *cfg : NV_FOLLOWUP_DEFAULTS };
}

uint32_t nova_followup_ms(const nv_followup_cfg_t *cfg, const nv_done_t *done)
{
    if(done && done->expects_answer) return cfg->answer_ms;
    if(done && done->outcome == NV_OUT_ERROR) return cfg->error_ms;
    return cfg->done_ms;
}

/* Remote mode: the pipeline owns the mic and the turn, so a tap only asks; a local mute or wake would change the
   picture, not the mic. */
static unsigned remote_tap(nova_fsm_t *f, nv_event_t ev)
{
    unsigned fx = 0;
    switch(ev) {
        case NV_EV_TAP_MIC: return NV_FX_REQ_MUTE;
        case NV_EV_WAKE_TAP:
        case NV_EV_TAP_ORB:
            if(!f->muted) return NV_FX_REQ_ARM;
            f->settling = false;                   /* muted: open the bar; the server would refuse to arm */
            set_view(f, NV_VIEW_OPEN, &fx);
            return fx;
        case NV_EV_TAP_END:
            f->settling = false;
            set_view(f, NV_VIEW_REST, &fx);
            return fx | NV_FX_REQ_CANCEL;
        default: return 0;                         /* wake word and push-to-talk belong to the audio client */
    }
}

unsigned nova_fsm_handle(nova_fsm_t *f, nv_event_t ev, const nv_done_t *done, uint32_t now)
{
    unsigned fx = 0;
    if(f->remote && (ev == NV_EV_TAP_MIC || ev == NV_EV_TAP_ORB || ev == NV_EV_TAP_END || ev == NV_EV_WAKE_TAP ||
                     ev == NV_EV_WAKE_KWS || ev == NV_EV_WAKE_PTT))
        return remote_tap(f, ev);
    switch(ev) {
        case NV_EV_WAKE_KWS:
        case NV_EV_WAKE_PTT:
            return f->muted ? 0 : wake(f, now);

        case NV_EV_WAKE_TAP:
        case NV_EV_TAP_ORB:
            if(!f->muted) return wake(f, now);
            f->settling = false;                   /* muted: open the bar, stay muted */
            set_view(f, NV_VIEW_OPEN, &fx);
            return fx;

        case NV_EV_TAP_MIC:
            f->muted = !f->muted;
            fx |= NV_FX_MUTE;
            if(f->muted) {
                if(f->activity != NV_IDLE) fx |= NV_FX_ABORT;
                set_activity(f, NV_IDLE, &fx);
                disarm(f);
                f->settling = false;
                f->user_speaking = false;
            }
            return fx;

        case NV_EV_TAP_END:
            if(f->activity != NV_IDLE) fx |= NV_FX_ABORT;
            set_activity(f, NV_IDLE, &fx);
            set_view(f, NV_VIEW_REST, &fx);
            disarm(f);
            f->settling = false;
            f->user_speaking = false;
            return fx;

        case NV_EV_TAP_TUCK:
            set_view(f, NV_VIEW_REST, &fx);
            return fx;

        case NV_EV_USER_SPEAKING_ON:
            if(f->muted || f->activity == NV_IDLE) return 0;
            f->user_speaking = true;
            if(busy(f)) {                          /* barge-in by speech */
                fx |= NV_FX_ABORT;
                set_activity(f, NV_LISTENING, &fx);
                arm(f, f->cfg.wake_ms, now);
            }
            return fx;

        case NV_EV_USER_SPEAKING_OFF:
            f->user_speaking = false;
            if(f->activity == NV_LISTENING && f->window_armed) f->window_deadline = now + f->window_ms;
            return 0;

        case NV_EV_TURN_COMMITTED:
            if(f->activity != NV_LISTENING) return 0;
            f->user_speaking = false;
            disarm(f);
            set_activity(f, NV_THINKING, &fx);
            return fx;

        case NV_EV_AGENT_THINKING:
        case NV_EV_AGENT_SPEAKING:
            if(f->activity == NV_IDLE) return 0;   /* stale event from an aborted turn */
            disarm(f);
            set_activity(f, ev == NV_EV_AGENT_THINKING ? NV_THINKING : NV_SPEAKING, &fx);
            return fx;

        case NV_EV_AGENT_DONE:
            if(!busy(f)) return 0;
            set_activity(f, NV_LISTENING, &fx);
            arm(f, nova_followup_ms(&f->cfg, done), now);
            return fx;

        case NV_EV_TICK:
            if(f->activity == NV_LISTENING && f->window_armed && !f->user_speaking && due(now, f->window_deadline)) {
                disarm(f);
                set_activity(f, NV_IDLE, &fx);
                f->settling = true;
                f->settle_deadline = now + f->cfg.settle_ms;
                return fx;
            }
            if(f->settling && due(now, f->settle_deadline)) {
                f->settling = false;
                set_view(f, NV_VIEW_REST, &fx);
            }
            return fx;
    }
    return fx;
}

unsigned nova_fsm_remote(nova_fsm_t *f, const char *state, uint32_t now)
{
    unsigned fx = 0;
    nv_activity_t a = NV_IDLE;
    f->remote = true;
    if(!strcmp(state, "ARMED") || !strcmp(state, "LISTENING")) a = NV_LISTENING;
    else if(!strcmp(state, "TRANSCRIBING") || !strcmp(state, "THINKING")) a = NV_THINKING;
    else if(!strcmp(state, "SPEAKING") || !strcmp(state, "INTERRUPTING")) a = NV_SPEAKING;
    disarm(f);   /* the server owns the windows; only the settle-to-rest runs locally */
    f->user_speaking = false;
    if(a == NV_IDLE) {
        if(f->activity != NV_IDLE) {
            f->settling = true;
            f->settle_deadline = now + f->cfg.settle_ms;
        }
        set_activity(f, NV_IDLE, &fx);
        return fx;
    }
    f->settling = false;
    set_view(f, NV_VIEW_OPEN, &fx);
    set_activity(f, a, &fx);
    return fx;
}

unsigned nova_fsm_remote_mute(nova_fsm_t *f, bool muted)
{
    f->remote = true;
    if(f->muted == muted) return 0;
    f->muted = muted;
    return NV_FX_MUTE;
}

const char *nova_activity_name(nv_activity_t a)
{
    switch(a) {
        case NV_IDLE: return "IDLE";
        case NV_LISTENING: return "LISTENING";
        case NV_THINKING: return "THINKING";
        case NV_SPEAKING: return "SPEAKING";
    }
    return "?";
}
