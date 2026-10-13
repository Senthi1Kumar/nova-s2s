#include "live/session_ctl.h"
#include "cb_app.h"
#include "live/live.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>

static struct {
    pthread_t thread;
    atomic_bool done;
    bool busy;
    unsigned next;             /* REQ bits tapped while a request was in flight */
    live_session_op_t op;
    bool want_muted, ok, muted_now;
    char err[160];
} s_req;

static void *worker(void *arg)
{
    (void)arg;
    bool m = s_req.want_muted;   /* a reply without "muted" leaves the asked-for state */
    s_req.ok = live_session_post(s_req.op, s_req.want_muted, &m, s_req.err, sizeof s_req.err);
    s_req.muted_now = m;
    atomic_store(&s_req.done, true);
    return NULL;
}

static const char *failure_title(void)
{
    switch(s_req.op) {
        case LIVE_SESSION_MUTE: return s_req.want_muted ? "Couldn't turn the mic off" : "Couldn't turn the mic on";
        case LIVE_SESSION_ARM: return "Couldn't wake Nova";
        case LIVE_SESSION_CANCEL: return "Couldn't stop Nova";
    }
    return "Nova didn't answer";
}

static void start(unsigned fx);

static void poll_cb(lv_timer_t *t)
{
    if(!atomic_load(&s_req.done)) return;
    pthread_join(s_req.thread, NULL);
    lv_timer_delete(t);
    s_req.busy = false;
    cb_app_nova_pending(false);
    if(s_req.ok && s_req.op == LIVE_SESSION_MUTE) {   /* the nova.mic frame says the same; the first one wins */
        cb_app_nova_apply(nova_fsm_remote_mute(cb_app_fsm(), s_req.muted_now));
    }
    else if(!s_req.ok) {
        fprintf(stderr, "live: %s: %s\n", failure_title(), s_req.err);
        live_toast(failure_title(), s_req.err);
    }
    unsigned next = s_req.next;
    s_req.next = 0;
    if(next) start(next);
}

static void start(unsigned fx)
{
    s_req.op = (fx & NV_FX_REQ_CANCEL) ? LIVE_SESSION_CANCEL : (fx & NV_FX_REQ_MUTE) ? LIVE_SESSION_MUTE : LIVE_SESSION_ARM;
    s_req.want_muted = !cb_app_fsm()->muted;
    s_req.err[0] = '\0';
    atomic_store(&s_req.done, false);
    if(pthread_create(&s_req.thread, NULL, worker, NULL) != 0) {
        live_toast(failure_title(), "no worker thread");
        return;
    }
    s_req.busy = true;
    cb_app_nova_pending(true);
    lv_timer_create(poll_cb, 50, NULL);
}

void session_ctl_request(unsigned fx)
{
    fx &= NV_FX_REQ_MUTE | NV_FX_REQ_ARM | NV_FX_REQ_CANCEL;
    if(!fx) return;
    if(s_req.busy) s_req.next = fx;
    else start(fx);
}

bool session_ctl_busy(void) { return s_req.busy; }
