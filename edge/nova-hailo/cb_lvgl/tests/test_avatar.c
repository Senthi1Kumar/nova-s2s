#include "cb_test.h"
#include "nova/avatar_model.h"
#include <math.h>
#include <string.h>

static float clamp01(float v) { return v < 0.0f ? 0.0f : v > 1.0f ? 1.0f : v; }
static bool is_near(float a, float b, float eps) { return fabsf(a - b) <= eps; }

/* Steps the model every 16 ms up to `until`; blinks in flight are allowed to finish. */
static uint32_t run_to(av_model_t *m, uint32_t from, uint32_t until)
{
    uint32_t t = from;
    while(t < until) av_model_step(m, t += 16);
    while(m->blinking) av_model_step(m, t += 16);
    return t;
}

static void target_is_state_plus_mood(void) {
    for(int s = 0; s < AV_STATE_COUNT; s++)
        for(int k = 0; k < AV_MOOD_COUNT; k++) {
            av_model_t m; av_model_init(&m, 7, 0);
            av_model_set_state(&m, (av_state_t)s, 0);
            av_model_set_mood(&m, (av_mood_t)k);
            av_pose_t b = av_state_base((av_state_t)s), d = av_mood_delta((av_mood_t)k), t = av_model_target(&m);
            bool muted = s == AV_ST_MUTED;
            CHECK(is_near(t.happy, muted ? 0.0f : clamp01(b.happy + d.happy), 1e-5f));
            CHECK(is_near(t.lidT, muted ? 0.0f : clamp01(b.lidT + d.lidT), 1e-5f));
            CHECK(is_near(t.lidB, clamp01(b.lidB + d.lidB), 1e-5f));
            CHECK(is_near(t.asym, muted ? 0.0f : b.asym + d.asym, 1e-5f));
            CHECK(is_near(t.tilt, b.tilt + d.tilt, 1e-5f));
            CHECK(is_near(t.lookX, b.lookX + d.lookX, 1e-5f));
            CHECK(is_near(t.lookY, b.lookY + d.lookY, 1e-5f));
            CHECK(is_near(t.eyeW, isnan(d.eyeW) ? b.eyeW : d.eyeW, 1e-5f));
            CHECK(is_near(t.eyeH, isnan(d.eyeH) ? b.eyeH : d.eyeH, 1e-5f));
            CHECK(is_near(t.hue, isnan(d.hue) ? b.hue : d.hue, 1e-5f));
            CHECK(is_near(t.sat, isnan(d.sat) ? b.sat : d.sat, 1e-5f));
            CHECK(is_near(t.bob, isnan(d.bob) ? b.bob : d.bob, 1e-5f));
            CHECK(is_near(t.lean, b.lean, 1e-5f) && is_near(t.spark, b.spark, 1e-5f));
        }
}
static void muted_clears_happy(void) {
    for(int k = 0; k < AV_MOOD_COUNT; k++) {
        av_model_t m; av_model_init(&m, 7, 0);
        av_model_set_state(&m, AV_ST_MUTED, 0);
        av_model_set_mood(&m, (av_mood_t)k);
        av_pose_t t = av_model_target(&m);
        CHECK(t.happy == 0.0f && t.asym == 0.0f && t.lidT == 0.0f);
        CHECK_EQ((int)t.sat, 5);
    }
}
static void happy_morph_never_both(void) {
    for(int i = 0; i <= 100; i++) {
        av_eye_t e = av_eye_morph(0.32f, (float)i * 0.01f);
        CHECK(!(e.pill_opa > 0.05f && e.arch_opa > 0.05f));
    }
    CHECK(is_near(av_eye_morph(0.32f, 0.15f).pill_h, 0.32f, 0.001f));
    CHECK(av_eye_morph(0.32f, 0.44f).pill_h < 0.2f * 0.32f);
    CHECK(av_eye_morph(0.32f, 0.46f).arch_rise < 0.15f);
    CHECK(is_near(av_eye_morph(0.32f, 0.0f).pill_h, 0.32f, 1e-5f));
    CHECK(is_near(av_eye_morph(0.32f, 1.0f).arch_rise, 1.0f, 1e-5f));
}
static void muted_ignores_replaced_mood_fields(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_MUTED, 0);
    av_pose_t d = { .eyeW = 0.3f, .eyeH = 0.5f, .hue = 30.0f, .sat = 60.0f, .bob = 2.0f, .happy = 0.5f, .asym = 0.2f,
                    .lidT = 0.4f };
    av_pose_t t = av_model_target_with(&m, d);
    CHECK_EQ((int)t.sat, 5); CHECK_EQ((int)t.hue, 214);
    CHECK(is_near(t.eyeH, 0.045f, 1e-5f) && is_near(t.eyeW, 0.18f, 1e-5f) && is_near(t.bob, 0.3f, 1e-5f));
    CHECK(t.happy == 0.0f && t.asym == 0.0f && t.lidT == 0.0f);
    av_model_set_state(&m, AV_ST_IDLE, 0);   /* awake, the same delta does apply */
    t = av_model_target_with(&m, d);
    CHECK_EQ((int)t.sat, 60); CHECK_EQ((int)t.hue, 30); CHECK(is_near(t.happy, 0.5f, 1e-5f));
}
static void reduce_motion_is_still(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_reduce_motion(&m, true);
    av_model_set_state(&m, AV_ST_THINKING, 0);
    av_frame_t f = av_model_step(&m, 16);
    av_pose_t t = av_model_target(&m);
    CHECK(is_near(f.p.hue, t.hue, 1e-4f) && is_near(f.p.eyeH, t.eyeH, 1e-4f));
    CHECK(av_model_settled(&m));
    for(uint32_t ms = 32; ms < 10000; ms += 16) {
        f = av_model_step(&m, ms);
        CHECK(f.hop == 0.0f && f.swell == 1.0f && f.nod == 1.0f && is_near(f.p.lookX, t.lookX, 1e-6f));
        CHECK(f.p.lidT == t.lidT);
    }
    av_model_set_state(&m, AV_ST_SPEAKING, 10000);
    av_model_set_level(&m, true, 0.0f);
    f = av_model_step(&m, 10016);
    CHECK(is_near(f.p.eyeH, av_model_target(&m).eyeH * (1.0f - 0.12f * 0.5f), 1e-4f));
    CHECK(f.nod == 1.0f && av_model_settled(&m));
    av_model_set_level(&m, true, 1.0f);
    CHECK(is_near(av_model_step(&m, 10032).p.eyeH, av_model_target(&m).eyeH * 0.88f, 1e-4f));
}
static void hue_takes_short_arc(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_GREETING, 0);
    uint32_t t = 0;
    while(t < 1400) av_model_step(&m, t += 16);
    CHECK(is_near(m.pose.hue, 26.0f, 0.5f));
    av_model_set_state(&m, AV_ST_THINKING, t);
    float h = av_model_step(&m, t + AV_TAU_MS).p.hue;
    CHECK(h < 60.0f || h > 220.0f);
    CHECK(h >= 0.0f && h < 360.0f);
}
static void base_table_values(void) {
    av_pose_t p = av_state_base(AV_ST_IDLE);
    CHECK(p.hue == 196.0f && p.sat == 40.0f && p.eyeW == 0.15f && p.eyeH == 0.32f && p.lidT == 0.08f);
    p = av_state_base(AV_ST_GREETING);
    CHECK(p.hue == 26.0f && p.spark == 1.0f && p.lidT == 0.0f);
    p = av_state_base(AV_ST_LISTENING);
    CHECK(p.eyeH == 0.32f && p.lidT == 0.08f);
    p = av_state_base(AV_ST_THINKING);
    CHECK(p.hue == 258.0f && p.lidB == 0.14f && p.lidT == 0.0f);
    p = av_state_base(AV_ST_SPEAKING);
    CHECK(p.lidT == 0.10f);
    p = av_state_base(AV_ST_MUTED);
    CHECK(p.eyeH == 0.045f && p.sat == 5.0f && p.lidT == 0.0f);
}
static void smooths_then_settles(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_THINKING, 0);
    av_pose_t from = av_state_base(AV_ST_IDLE), to = av_model_target(&m);
    av_frame_t f = av_model_step(&m, AV_TAU_MS);
    float k = (f.p.hue - from.hue) / (to.hue - from.hue);
    CHECK(is_near(k, 1.0f - expf(-1.0f), 0.05f));
    CHECK(!av_model_settled(&m));
    av_model_set_state(&m, AV_ST_IDLE, AV_TAU_MS);
    CHECK(!av_model_settled(&m));
    run_to(&m, AV_TAU_MS, 1500);
    CHECK(av_model_settled(&m));
}
static void settles_after_flapping(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    uint32_t t = 0;
    for(int i = 0; i < 125; i++) {
        av_model_set_state(&m, i & 1 ? AV_ST_SPEAKING : AV_ST_LISTENING, t);
        av_model_step(&m, t += 16);
    }
    av_model_set_state(&m, AV_ST_IDLE, t);
    CHECK(!av_model_settled(&m));
    run_to(&m, t, t + 1200);
    CHECK(av_model_settled(&m));
}
static void blink_after_smoothing(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    uint32_t w = av_model_next_wake(&m);
    CHECK(w >= 3000 && w <= 6000);
    av_frame_t f = av_model_step(&m, w + AV_BLINK_MS / 2);
    CHECK(f.p.lidT > 0.9f);
    f = av_model_step(&m, w + AV_BLINK_MS + 1);
    CHECK(is_near(f.p.lidT, 0.08f, 0.03f));   /* back to the relaxed idle lid */
    CHECK(av_model_next_wake(&m) >= w + AV_BLINK_MS + 3000);

    av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_MUTED, 0);
    for(uint32_t t = 16; t <= 10000; t += 16) {   /* the idle lid opens fully, then no blink ever closes it */
        float lid = av_model_step(&m, t).p.lidT;
        CHECK(lid <= 0.08f + 1e-4f);
        if(t >= 2000) CHECK(lid < 0.001f);
    }
}
static void greeting_reverts(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_LISTENING, 0);
    uint32_t t = 0;
    while(t < 500) av_model_step(&m, t += 16);
    av_model_set_state(&m, AV_ST_GREETING, t);
    uint32_t t0 = t;
    while(t < t0 + AV_GREET_MS / 2) av_model_step(&m, t += 16);
    CHECK_EQ(m.state, AV_ST_GREETING);
    CHECK(av_model_step(&m, t += 16).p.spark > 0.5f);
    while(t < t0 + AV_GREET_MS + 1) av_model_step(&m, t += 16);
    CHECK_EQ(m.state, AV_ST_LISTENING);

    av_model_set_state(&m, AV_ST_MUTED, t);   /* a muted Nova does not greet */
    av_model_set_state(&m, AV_ST_GREETING, t);
    CHECK_EQ(m.state, AV_ST_MUTED);
}
static void live_levels_drive_the_frame(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_SPEAKING, 0);
    av_model_set_level(&m, true, 1.0f);
    av_frame_t f = av_model_step(&m, 3000);
    CHECK(is_near(f.nod, 0.98f, 1e-4f));
    CHECK(f.p.eyeH < av_model_target(&m).eyeH);
    av_model_set_duck(&m, 0.0f);
    f = av_model_step(&m, 3016);
    CHECK(is_near(f.nod, 1.0f, 1e-4f));
    av_model_set_state(&m, AV_ST_LISTENING, 3016);
    av_model_set_level(&m, false, 1.0f);
    CHECK(is_near(av_model_step(&m, 3032).swell, 1.025f, 1e-4f));
    av_model_set_level(&m, false, 0.0f);   /* synthesised envelope: it moves */
    float a = av_model_step(&m, 4000).swell, b = av_model_step(&m, 4140).swell;
    CHECK(a > 1.0f && fabsf(a - b) > 1e-4f);
    av_model_set_state(&m, AV_ST_IDLE, 4140);
    f = av_model_step(&m, 4156);
    CHECK(is_near(f.swell, 1.0f, 1e-6f) && is_near(f.nod, 1.0f, 1e-6f));
}
static void thinking_scans_sideways(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_state(&m, AV_ST_THINKING, 0);
    uint32_t t = 0;
    float lo = 9, hi = -9;
    while(t < 6000) {
        float x = av_model_step(&m, t += 16).p.lookX;
        if(t > 2000) { if(x < lo) lo = x; if(x > hi) hi = x; }
    }
    CHECK(hi - lo > 0.1f);
}
static void heads_and_voices(void) {
    CHECK_EQ(av_head_for_voice("en_US-amy-low"), AV_HEAD_PEBBLE);
    CHECK_EQ(av_head_for_voice("en_US-lessac-medium"), AV_HEAD_DROP);
    CHECK_EQ(av_head_for_voice("en_US-ryan-high"), AV_HEAD_GEM);
    CHECK_EQ(av_head_for_voice("en_GB-alba-medium"), AV_HEAD_CLOUD);
    CHECK_EQ(av_head_for_voice("en_US-hfc_female-medium"), AV_HEAD_CAPSULE);
    CHECK_EQ(av_head_for_voice("en_US-joe-medium"), AV_HEAD_PEBBLE);
    CHECK_EQ(av_head_for_voice(NULL), AV_HEAD_PEBBLE);
    CHECK_EQ(av_head_parse("gem"), AV_HEAD_GEM);
    CHECK_EQ(av_head_parse("voice"), AV_HEAD_COUNT);
    CHECK_EQ(av_head_parse("nope"), AV_HEAD_COUNT);
    CHECK_EQ(av_head_parse(NULL), AV_HEAD_COUNT);
    for(int h = 0; h < AV_HEAD_COUNT; h++) CHECK_EQ(av_head_parse(av_head_id((av_head_t)h)), h);
    CHECK(!strcmp(av_head_id(AV_HEAD_PEBBLE), "pebble") && !strcmp(av_head_id(AV_HEAD_EYES), "eyes"));
}
static void state_for_activity(void) {
    CHECK_EQ(av_state_for(NV_IDLE, false), AV_ST_IDLE);
    CHECK_EQ(av_state_for(NV_LISTENING, false), AV_ST_LISTENING);
    CHECK_EQ(av_state_for(NV_THINKING, false), AV_ST_THINKING);
    CHECK_EQ(av_state_for(NV_SPEAKING, false), AV_ST_SPEAKING);
    CHECK_EQ(av_state_for(NV_SPEAKING, true), AV_ST_MUTED);
}
int main(void) {
    RUN(target_is_state_plus_mood); RUN(muted_clears_happy); RUN(happy_morph_never_both);
    RUN(smooths_then_settles); RUN(settles_after_flapping); RUN(blink_after_smoothing);
    RUN(greeting_reverts); RUN(live_levels_drive_the_frame); RUN(thinking_scans_sideways);
    RUN(heads_and_voices); RUN(state_for_activity);
    RUN(muted_ignores_replaced_mood_fields); RUN(reduce_motion_is_still); RUN(hue_takes_short_arc);
    RUN(base_table_values);
    DONE();
}
