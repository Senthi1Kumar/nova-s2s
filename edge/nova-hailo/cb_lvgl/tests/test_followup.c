#include "cb_test.h"
#include "nova/nova_fsm.h"

static const nv_done_t DONE_OK = { false, NV_OUT_DONE }, ASK = { true, NV_OUT_DONE }, ERR = { false, NV_OUT_ERROR };

static void policy(void) {
    nv_followup_cfg_t c = NV_FOLLOWUP_DEFAULTS;
    CHECK_EQ(nova_followup_ms(&c, &ASK), 8000); CHECK_EQ(nova_followup_ms(&c, &ERR), 4000);
    CHECK_EQ(nova_followup_ms(&c, &DONE_OK), 2000);
    nv_done_t ask_err = { true, NV_OUT_ERROR };
    CHECK_EQ(nova_followup_ms(&c, &ask_err), 8000);   /* a question wins over an error */
}
static nova_fsm_t after_done(const nv_done_t *d, uint32_t t) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, t);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, t + 10);
    nova_fsm_handle(&f, NV_EV_AGENT_DONE, d, t + 20);
    return f;
}
static void expiry_then_settle(void) {
    nova_fsm_t f = after_done(&DONE_OK, 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 2019), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 2020), NV_FX_ACTIVITY);
    CHECK_EQ(f.activity, NV_IDLE); CHECK_EQ(f.view, NV_VIEW_OPEN);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 2719), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 2720), NV_FX_VIEW);
    CHECK_EQ(f.view, NV_VIEW_REST); CHECK(!f.settling);
}
static void error_and_ask_windows(void) {
    nova_fsm_t f = after_done(&ERR, 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 4019), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 4020), NV_FX_ACTIVITY);
    nova_fsm_t g = after_done(&ASK, 0);
    CHECK_EQ(nova_fsm_handle(&g, NV_EV_TICK, NULL, 8019), 0);
    CHECK_EQ(nova_fsm_handle(&g, NV_EV_TICK, NULL, 8020), NV_FX_ACTIVITY);
}
static void paused_while_user_speaks(void) {
    nova_fsm_t f = after_done(&DONE_OK, 0);
    nova_fsm_handle(&f, NV_EV_USER_SPEAKING_ON, NULL, 1000);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 9000), 0);       /* would have expired */
    nova_fsm_handle(&f, NV_EV_USER_SPEAKING_OFF, NULL, 9000);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 9000 + 1999), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 9000 + 2000), NV_FX_ACTIVITY);
}
static void wake_window_expires(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 100);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 8099), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 8100), NV_FX_ACTIVITY);
}
static void wake_cancels_settle(void) {
    nova_fsm_t f = after_done(&DONE_OK, 0);
    nova_fsm_handle(&f, NV_EV_TICK, NULL, 2020);              /* IDLE, settling */
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 2100);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, 2800) & NV_FX_VIEW, 0);
    CHECK_EQ(f.view, NV_VIEW_OPEN); CHECK_EQ(f.activity, NV_LISTENING);
}
static void wrap_safe_deadline(void) {
    uint32_t t0 = 0xFFFFFFFFu - 1000u;                          /* window crosses the uint32 wrap */
    nova_fsm_t f = after_done(&DONE_OK, t0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, t0 + 1500u), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TICK, NULL, t0 + 20u + 2000u), NV_FX_ACTIVITY);
}
int main(void) {
    RUN(policy); RUN(expiry_then_settle); RUN(error_and_ask_windows); RUN(paused_while_user_speaks);
    RUN(wake_window_expires); RUN(wake_cancels_settle); RUN(wrap_safe_deadline);
    DONE();
}
