#include "cb_test.h"
#include "cb_demo.h"
#include <string.h>

/* A recording fake io wired to a real nova_fsm. */
typedef struct {
    nova_fsm_t fsm;
    uint32_t now;
    char log[64][48];
    int n_log;
    int applies[64];
    int n_applies;
    int wakes;
    int wake_not_resting;
    int agent_while_idle;
    unsigned force_fx_on;   /* event that returns NV_FX_ABORT (for the abort-by-emit test); 0xFF = none */
    bool not_live;
    int wakes_at_first_reset;
} fake_t;

static void logf_(fake_t *f, const char *s) { if(f->n_log < 64) { snprintf(f->log[f->n_log], sizeof f->log[0], "%s", s); f->n_log++; } }

static unsigned f_emit(void *ctx, nv_event_t ev, const nv_done_t *done) {
    fake_t *f = ctx;
    char b[48];
    snprintf(b, sizeof b, "ev%d", (int)ev);
    logf_(f, b);
    if((ev == NV_EV_AGENT_THINKING || ev == NV_EV_AGENT_SPEAKING || ev == NV_EV_AGENT_DONE) && f->fsm.activity == NV_IDLE)
        f->agent_while_idle++;
    if(ev == NV_EV_WAKE_KWS) {
        f->wakes++;
        if(f->fsm.view != NV_VIEW_REST) f->wake_not_resting++;
    }
    if((unsigned)ev == f->force_fx_on) return NV_FX_ABORT;
    return nova_fsm_handle(&f->fsm, ev, done, f->now);
}
static void f_say(void *ctx, const char *text, bool user) {
    fake_t *f = ctx;
    char b[48];
    snprintf(b, sizeof b, "%s:%s", user ? "U" : "N", text);
    logf_(f, b);
}
static void f_apply(void *ctx, int id) {
    fake_t *f = ctx;
    if(f->n_applies < 64) f->applies[f->n_applies++] = id;
    if(id == APPLY_RESET && f->wakes_at_first_reset < 0) f->wakes_at_first_reset = f->wakes;
    char b[48];
    snprintf(b, sizeof b, "apply%d", id);
    logf_(f, b);
}
static bool f_live(void *ctx) { fake_t *f = ctx; return !f->not_live && f->fsm.activity != NV_IDLE; }

static cb_demo_io_t io_for(fake_t *f) { return (cb_demo_io_t){ f, f_emit, f_say, f_apply, f_live }; }
static void fake_init(fake_t *f) {
    memset(f, 0, sizeof *f);
    nova_fsm_init(&f->fsm, NULL);
    f->force_fx_on = 0xFF;
    f->wakes_at_first_reset = -1;
}

#define EV(e, ms)        { .kind = DS_EVENT, .ev = (e), .after_ms = (ms) }
#define EV_START(e, ms)  { .kind = DS_EVENT, .ev = (e), .after_ms = (ms), .turn_start = true }
#define USER(t, ms)      { .kind = DS_USER, .text = (t), .after_ms = (ms) }
#define APPLY(id, ms)    { .kind = DS_APPLY, .apply_id = (id), .after_ms = (ms) }

static const cb_step_t two_turns[] = {
    EV_START(NV_EV_WAKE_KWS, 100), USER("one", 200), APPLY(1, 300),
    EV_START(NV_EV_WAKE_KWS, 100), USER("two", 200), APPLY(2, 300),
};
#define N2 (sizeof two_turns / sizeof two_turns[0])

static void runs_steps_in_order_at_their_times(void) {
    fake_t f; fake_init(&f);
    cb_demo_t d;
    cb_demo_init(&d, two_turns, N2, io_for(&f), 0, 50);
    cb_demo_tick(&d, 49);  CHECK_EQ(f.n_log, 0);
    cb_demo_tick(&d, 50);  CHECK_EQ(f.n_log, 1); CHECK(strcmp(f.log[0], "ev0") == 0);
    cb_demo_tick(&d, 149); CHECK_EQ(f.n_log, 1);
    cb_demo_tick(&d, 150); CHECK_EQ(f.n_log, 2); CHECK(strcmp(f.log[1], "U:one") == 0);
    cb_demo_tick(&d, 350); CHECK_EQ(f.n_applies, 1); CHECK_EQ(f.applies[0], 1);
}
static void paused_runs_nothing(void) {
    fake_t f; fake_init(&f);
    cb_demo_t d;
    cb_demo_init(&d, two_turns, N2, io_for(&f), 0, 0);
    cb_demo_set_paused(&d, true);
    cb_demo_tick(&d, 10000); CHECK_EQ(f.n_log, 0);
    cb_demo_set_paused(&d, false);
    cb_demo_tick(&d, 10001); CHECK(f.n_log > 0);
}
static void abort_jumps_to_next_turn_start(void) {
    fake_t f; fake_init(&f);
    cb_demo_t d;
    cb_demo_init(&d, two_turns, N2, io_for(&f), 0, 0);
    cb_demo_tick(&d, 0);                    /* wake of turn 1 */
    cb_demo_abort(&d, 50);
    CHECK_EQ(d.run_id, 1);
    CHECK(d.steps[d.i].turn_start);
    cb_demo_tick(&d, 1549); int before = f.n_log;
    cb_demo_tick(&d, 1550); CHECK_EQ(f.n_log, before + 1);
    cb_demo_tick(&d, 1650);                 /* "two" */
    CHECK(strcmp(f.log[f.n_log - 1], "U:two") == 0);
}
static void abort_effect_from_emit_aborts(void) {
    fake_t f; fake_init(&f);
    f.force_fx_on = NV_EV_WAKE_KWS;
    cb_demo_t d;
    cb_demo_init(&d, two_turns, N2, io_for(&f), 0, 0);
    cb_demo_tick(&d, 0);
    CHECK_EQ(d.run_id, 1);
    CHECK(d.steps[d.i].turn_start);
}
static void not_live_skips_the_turn(void) {
    fake_t f; fake_init(&f);
    f.not_live = true;
    cb_demo_t d;
    cb_demo_init(&d, two_turns, N2, io_for(&f), 0, 0);
    for(uint32_t t = 0; t < 5000; t += 10) cb_demo_tick(&d, t);
    CHECK_EQ(f.n_applies, 0);
    for(int i = 0; i < f.n_log; i++) CHECK(f.log[i][0] != 'U');
}
static void full_script_two_loops(void) {
    fake_t f; fake_init(&f);
    cb_demo_t d;
    cb_demo_init(&d, cb_demo_script, cb_demo_script_len, io_for(&f), 0, 2000);
    for(f.now = 0; f.now < 2u * 70000u; f.now += 10) {
        if(f.now % 100 == 0) nova_fsm_handle(&f.fsm, NV_EV_TICK, NULL, f.now);
        cb_demo_tick(&d, f.now);
    }
    int wakes_first_loop = f.wakes_at_first_reset;
    CHECK(f.n_applies >= 16);
    for(int i = 0; i < 16; i++) CHECK_EQ(f.applies[i], (i % 8) + 1);   /* two identical loops, RESET last */
    CHECK_EQ(f.agent_while_idle, 0);
    CHECK_EQ(f.wake_not_resting, 0);
    CHECK_EQ(wakes_first_loop, 5);          /* follow-up turns need no wake word */
    CHECK_EQ(d.run_id, 0);                  /* the script never aborts itself */
}
int main(void) {
    RUN(runs_steps_in_order_at_their_times); RUN(paused_runs_nothing); RUN(abort_jumps_to_next_turn_start);
    RUN(abort_effect_from_emit_aborts); RUN(not_live_skips_the_turn); RUN(full_script_two_loops);
    DONE();
}
