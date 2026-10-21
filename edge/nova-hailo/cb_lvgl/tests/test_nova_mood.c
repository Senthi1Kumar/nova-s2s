#include "cb_test.h"
#include "nova/nova_mood.h"

static av_mood_t after(nm_event_t ev, uint32_t t)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, ev, t);
    return nova_mood_get(&m, t);
}

static void init_is_calm(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 500);
    CHECK_EQ(nova_mood_get(&m, 500), AV_CALM);
}

static void table_rows(void)
{
    CHECK_EQ(after(NM_SESSION_START, 10), AV_HAPPY);
    CHECK_EQ(after(NM_SPEECH_STARTED, 10), AV_CURIOUS);
    CHECK_EQ(after(NM_TRANSCRIPT_DONE, 10), AV_CALM);
    CHECK_EQ(after(NM_TOOL_ERROR, 10), AV_CONCERNED);
    CHECK_EQ(after(NM_REPLY_DELTA, 10), AV_HAPPY);
    CHECK_EQ(after(NM_RESPONSE_DONE, 10), AV_CALM);
}

static void duck_then_unduck_restores(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_DUCK, 20);
    CHECK_EQ(nova_mood_get(&m, 20), AV_CURIOUS);
    CHECK_EQ(nova_mood_get(&m, 4000), AV_CURIOUS);
    nova_mood_event(&m, NM_UNDUCK, 4010);
    CHECK_EQ(nova_mood_get(&m, 4010), AV_HAPPY);
}

static void duck_twice_keeps_first_saved(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_DUCK, 20);
    nova_mood_event(&m, NM_DUCK, 30);
    nova_mood_event(&m, NM_UNDUCK, 40);
    CHECK_EQ(nova_mood_get(&m, 40), AV_HAPPY);
}

static void event_during_duck_wins(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_DUCK, 20);
    nova_mood_event(&m, NM_TOOL_ERROR, 30);
    nova_mood_event(&m, NM_UNDUCK, 40);
    CHECK_EQ(nova_mood_get(&m, 40), AV_CONCERNED);
}

static void interrupted_surprise_then_calm(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_INTERRUPTED, 100);
    CHECK_EQ(nova_mood_get(&m, 100), AV_SURPRISED);
    CHECK_EQ(nova_mood_get(&m, 100 + NM_SURPRISE_MS - 1), AV_SURPRISED);
    CHECK_EQ(nova_mood_get(&m, 100 + NM_SURPRISE_MS), AV_CALM);
    CHECK_EQ(nova_mood_get(&m, 100 + NM_SURPRISE_MS + 5), AV_CALM);
}

static void interrupted_cancels_duck(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_DUCK, 20);
    nova_mood_event(&m, NM_INTERRUPTED, 30);
    nova_mood_event(&m, NM_UNDUCK, 40);
    CHECK_EQ(nova_mood_get(&m, 40), AV_SURPRISED);
    CHECK_EQ(nova_mood_get(&m, 30 + NM_SURPRISE_MS), AV_CALM);
}

static void later_event_ends_surprise(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_INTERRUPTED, 100);
    nova_mood_event(&m, NM_SPEECH_STARTED, 200);
    CHECK_EQ(nova_mood_get(&m, 200), AV_CURIOUS);
}

static void error_turn_stays_concerned(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_TOOL_ERROR, 10);
    nova_mood_event(&m, NM_REPLY_DELTA, 20);
    CHECK_EQ(nova_mood_get(&m, 20), AV_CONCERNED);
    nova_mood_event(&m, NM_REPLY_DELTA, 30);
    CHECK_EQ(nova_mood_get(&m, 30), AV_CONCERNED);
    nova_mood_event(&m, NM_RESPONSE_DONE, 40);
    CHECK_EQ(nova_mood_get(&m, 40), AV_CALM);
    nova_mood_event(&m, NM_REPLY_DELTA, 50);
    CHECK_EQ(nova_mood_get(&m, 50), AV_HAPPY);
}

static void sleepy_after_quiet(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 1000);
    CHECK_EQ(nova_mood_get(&m, 1000 + NM_SLEEPY_MS - 1), AV_CALM);
    CHECK_EQ(nova_mood_get(&m, 1000 + NM_SLEEPY_MS), AV_SLEEPY);
    nova_mood_event(&m, NM_SPEECH_STARTED, 1000 + NM_SLEEPY_MS + 1);
    CHECK_EQ(nova_mood_get(&m, 1000 + NM_SLEEPY_MS + 1), AV_CURIOUS);
}

static void sleepy_counts_from_last_speech_or_reply(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 1000);
    nova_mood_event(&m, NM_RESPONSE_DONE, 2000);
    CHECK_EQ(nova_mood_get(&m, 1000 + NM_SLEEPY_MS - 1), AV_CALM);
    CHECK_EQ(nova_mood_get(&m, 1000 + NM_SLEEPY_MS), AV_SLEEPY);
}

static void other_event_wakes_sleepy(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    CHECK_EQ(nova_mood_get(&m, NM_SLEEPY_MS), AV_SLEEPY);
    nova_mood_event(&m, NM_TRANSCRIPT_DONE, NM_SLEEPY_MS + 5);
    CHECK_EQ(nova_mood_get(&m, NM_SLEEPY_MS + 5), AV_CALM);
    CHECK_EQ(nova_mood_get(&m, NM_SLEEPY_MS + 50), AV_CALM);
}

static void wraps_around_u32(void)
{
    nova_mood_t m;
    uint32_t t0 = 0xFFFFFF00u;
    nova_mood_init(&m, t0);
    CHECK_EQ(nova_mood_get(&m, t0 + NM_SLEEPY_MS - 1), AV_CALM);
    CHECK_EQ(nova_mood_get(&m, t0 + NM_SLEEPY_MS), AV_SLEEPY);
    nova_mood_event(&m, NM_INTERRUPTED, t0 + 10);
    CHECK_EQ(nova_mood_get(&m, t0 + 10 + NM_SURPRISE_MS - 1), AV_SURPRISED);
    CHECK_EQ(nova_mood_get(&m, t0 + 10 + NM_SURPRISE_MS), AV_CALM);
}

static void interrupt_clears_turn_error(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_TOOL_ERROR, 10);
    nova_mood_event(&m, NM_INTERRUPTED, 20);
    nova_mood_event(&m, NM_REPLY_DELTA, 20 + NM_SURPRISE_MS);
    CHECK_EQ(nova_mood_get(&m, 20 + NM_SURPRISE_MS), AV_HAPPY);
}

static void speech_clears_turn_error(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_TOOL_ERROR, 10);
    nova_mood_event(&m, NM_SPEECH_STARTED, 20);
    nova_mood_event(&m, NM_REPLY_DELTA, 30);
    CHECK_EQ(nova_mood_get(&m, 30), AV_HAPPY);
}

static void duck_survives_streaming_reply(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_DUCK, 10);
    for(uint32_t t = 20; t <= 40; t += 10) {
        nova_mood_event(&m, NM_REPLY_DELTA, t);
        CHECK_EQ(nova_mood_get(&m, t), AV_CURIOUS);
    }
    nova_mood_event(&m, NM_UNDUCK, 50);
    CHECK_EQ(nova_mood_get(&m, 50), AV_HAPPY);
}

static void tool_error_during_duck_restores_concerned(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_DUCK, 10);
    nova_mood_event(&m, NM_TOOL_ERROR, 20);
    CHECK_EQ(nova_mood_get(&m, 20), AV_CURIOUS);
    nova_mood_event(&m, NM_REPLY_DELTA, 30);
    CHECK_EQ(nova_mood_get(&m, 30), AV_CURIOUS);
    nova_mood_event(&m, NM_UNDUCK, 40);
    CHECK_EQ(nova_mood_get(&m, 40), AV_CONCERNED);
}

static void unduck_without_duck_is_noop(void)
{
    nova_mood_t m;
    nova_mood_init(&m, 0);
    nova_mood_event(&m, NM_REPLY_DELTA, 10);
    nova_mood_event(&m, NM_UNDUCK, 20);
    CHECK_EQ(nova_mood_get(&m, 20), AV_HAPPY);
}

int main(void)
{
    RUN(init_is_calm);
    RUN(table_rows);
    RUN(duck_then_unduck_restores);
    RUN(duck_twice_keeps_first_saved);
    RUN(event_during_duck_wins);
    RUN(interrupted_surprise_then_calm);
    RUN(interrupted_cancels_duck);
    RUN(later_event_ends_surprise);
    RUN(error_turn_stays_concerned);
    RUN(sleepy_after_quiet);
    RUN(sleepy_counts_from_last_speech_or_reply);
    RUN(other_event_wakes_sleepy);
    RUN(wraps_around_u32);
    RUN(interrupt_clears_turn_error);
    RUN(speech_clears_turn_error);
    RUN(duck_survives_streaming_reply);
    RUN(tool_error_during_duck_restores_concerned);
    RUN(unduck_without_duck_is_noop);
    DONE();
}
