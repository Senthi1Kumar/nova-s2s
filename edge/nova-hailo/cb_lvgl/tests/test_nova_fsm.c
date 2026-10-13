#include "cb_test.h"
#include "nova/nova_fsm.h"

static const nv_done_t DONE_OK = { false, NV_OUT_DONE }, ASK = { true, NV_OUT_DONE };

static void wake_from_rest_opens_and_listens(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    CHECK_EQ(f.view, NV_VIEW_REST); CHECK_EQ(f.activity, NV_IDLE);
    unsigned fx = nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 1000);
    CHECK_EQ(fx, NV_FX_VIEW | NV_FX_ACTIVITY | NV_FX_CHIME);
    CHECK_EQ(f.view, NV_VIEW_OPEN); CHECK_EQ(f.activity, NV_LISTENING);
    CHECK(f.window_armed); CHECK_EQ(f.window_deadline, 1000 + 8000);
}
static void wake_while_speaking_barges_in(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, 100);
    nova_fsm_handle(&f, NV_EV_AGENT_SPEAKING, NULL, 200);
    unsigned fx = nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 300);
    CHECK(fx & NV_FX_ABORT); CHECK(!(fx & NV_FX_CHIME)); CHECK_EQ(f.activity, NV_LISTENING);
}
static void wake_while_listening_restarts_window(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 500), 0);
    CHECK_EQ(f.window_deadline, 500 + 8000);
}
static void muted_kws_ignored_tap_opens_still_muted(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 0);
    CHECK(f.muted);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 10), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_PTT, NULL, 10), 0);
    CHECK_EQ(f.view, NV_VIEW_REST);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_ORB, NULL, 20), NV_FX_VIEW);
    CHECK_EQ(f.view, NV_VIEW_OPEN); CHECK(f.muted); CHECK_EQ(f.activity, NV_IDLE);
}
static void mute_mid_turn_aborts(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, 100);
    unsigned fx = nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 200);
    CHECK_EQ(fx, NV_FX_MUTE | NV_FX_ABORT | NV_FX_ACTIVITY);
    CHECK_EQ(f.activity, NV_IDLE); CHECK_EQ(f.view, NV_VIEW_OPEN); CHECK(!f.window_armed);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 300), NV_FX_MUTE);   /* unmute: nothing else */
    CHECK(!f.muted); CHECK_EQ(f.activity, NV_IDLE);
}
static void end_rests_keeps_mute(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 10);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_END, NULL, 20), NV_FX_VIEW);   /* already IDLE: no ABORT */
    CHECK_EQ(f.view, NV_VIEW_REST); CHECK(f.muted);
}
static void end_mid_turn_aborts(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    unsigned fx = nova_fsm_handle(&f, NV_EV_TAP_END, NULL, 20);
    CHECK_EQ(fx, NV_FX_ABORT | NV_FX_ACTIVITY | NV_FX_VIEW);
    CHECK_EQ(f.activity, NV_IDLE); CHECK(!f.window_armed);
}
static void tuck_keeps_conversation(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, 100);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_TUCK, NULL, 200), NV_FX_VIEW);
    CHECK_EQ(f.activity, NV_THINKING); CHECK_EQ(f.view, NV_VIEW_REST);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_TUCK, NULL, 210), 0);
}
static void agent_events_ignored_when_idle(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_AGENT_THINKING, NULL, 0), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_AGENT_SPEAKING, NULL, 0), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_AGENT_DONE, &DONE_OK, 0), 0);
    CHECK_EQ(f.activity, NV_IDLE); CHECK_EQ(f.view, NV_VIEW_REST);
}
static void speech_ignored_when_idle(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_USER_SPEAKING_ON, NULL, 0), 0);
    CHECK(!f.user_speaking);
}
static void barge_in_by_speech(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, 10);
    nova_fsm_handle(&f, NV_EV_AGENT_SPEAKING, NULL, 20);
    unsigned fx = nova_fsm_handle(&f, NV_EV_USER_SPEAKING_ON, NULL, 30);
    CHECK(fx & NV_FX_ABORT); CHECK_EQ(f.activity, NV_LISTENING); CHECK(f.user_speaking);
}
static void full_turn(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL);
    nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0);
    nova_fsm_handle(&f, NV_EV_USER_SPEAKING_ON, NULL, 100);
    nova_fsm_handle(&f, NV_EV_USER_SPEAKING_OFF, NULL, 900);
    nova_fsm_handle(&f, NV_EV_TURN_COMMITTED, NULL, 950);
    CHECK_EQ(f.activity, NV_THINKING); CHECK(!f.window_armed);
    nova_fsm_handle(&f, NV_EV_AGENT_THINKING, NULL, 1000);
    nova_fsm_handle(&f, NV_EV_AGENT_SPEAKING, NULL, 1500);
    CHECK_EQ(f.activity, NV_SPEAKING);
    nova_fsm_handle(&f, NV_EV_AGENT_DONE, &ASK, 3000);
    CHECK_EQ(f.activity, NV_LISTENING); CHECK_EQ(f.window_deadline, 3000 + 8000);
}
static void remote_taps_only_request(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL); f.remote = true;   /* live before the first nova.fsm frame */
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 0), NV_FX_REQ_MUTE);
    CHECK(!f.muted);                                           /* only the server's nova.mic frame mutes */
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_ORB, NULL, 0), NV_FX_REQ_ARM);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_TAP, NULL, 0), NV_FX_REQ_ARM);
    CHECK_EQ(f.activity, NV_IDLE); CHECK_EQ(f.view, NV_VIEW_REST);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_KWS, NULL, 0), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_WAKE_PTT, NULL, 0), 0);
    nova_fsm_remote(&f, "SPEAKING", 10);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_END, NULL, 20), NV_FX_REQ_CANCEL | NV_FX_VIEW);
    CHECK_EQ(f.activity, NV_SPEAKING); CHECK_EQ(f.view, NV_VIEW_REST);   /* the server's IDLE ends the turn */
}
static void remote_mute_follows_server(void) {
    nova_fsm_t f; nova_fsm_init(&f, NULL); f.remote = true;
    nova_fsm_remote(&f, "SPEAKING", 0);
    CHECK_EQ(nova_fsm_remote_mute(&f, true), NV_FX_MUTE);
    CHECK(f.muted); CHECK_EQ(f.activity, NV_SPEAKING);         /* mic off only: Nova may finish its sentence */
    CHECK_EQ(nova_fsm_remote_mute(&f, true), 0);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 10), NV_FX_REQ_MUTE);   /* the caller asks for !muted */
    nova_fsm_remote(&f, "IDLE", 20);
    nova_fsm_handle(&f, NV_EV_TICK, NULL, 20 + f.cfg.settle_ms);
    CHECK_EQ(f.view, NV_VIEW_REST);
    CHECK_EQ(nova_fsm_handle(&f, NV_EV_TAP_ORB, NULL, 30), NV_FX_VIEW);   /* muted: open, no arm request */
    CHECK_EQ(nova_fsm_remote_mute(&f, false), NV_FX_MUTE);
    CHECK(!f.muted);
}
int main(void) {
    RUN(wake_from_rest_opens_and_listens); RUN(wake_while_speaking_barges_in); RUN(wake_while_listening_restarts_window);
    RUN(muted_kws_ignored_tap_opens_still_muted); RUN(mute_mid_turn_aborts); RUN(end_rests_keeps_mute);
    RUN(end_mid_turn_aborts); RUN(tuck_keeps_conversation); RUN(agent_events_ignored_when_idle);
    RUN(speech_ignored_when_idle); RUN(barge_in_by_speech); RUN(full_turn);
    RUN(remote_taps_only_request); RUN(remote_mute_follows_server);
    DONE();
}
