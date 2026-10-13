#include "cb_test.h"
#include "nova/avatar_model.h"
#include <math.h>

static bool near(float a, float b) { return fabsf(a - b) < 0.02f; }

static void starts_idle_and_still(void) {
    av_model_t m; av_model_init(&m, 7, 1000);
    CHECK_EQ(m.mood, AV_IDLE);
    av_pose_t p = av_model_step(&m, 1000);
    CHECK(near(p.eye_open, av_mood_base(AV_IDLE).eye_open));
    CHECK(!av_model_animating(&m, 1000 + AV_BLEND_MS));   /* idle runs on the ambient clock */
}
static void blinks_close_then_reopen(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    uint32_t b = m.next_blink;
    CHECK(b >= 2600 && b <= 6000);
    av_model_step(&m, b);
    CHECK(av_model_animating(&m, b));
    CHECK(av_model_step(&m, b + AV_BLINK_MS / 2).eye_open < 0.1f);
    CHECK(near(av_model_step(&m, b + AV_BLINK_MS + 1).eye_open, av_mood_base(AV_IDLE).eye_open));
    CHECK(m.next_blink > b + AV_BLINK_MS);
}
static void mood_cross_fades(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_mood(&m, AV_SPEAK, 100);
    av_pose_t a = av_model_step(&m, 100), b = av_mood_base(AV_IDLE);
    CHECK(near(a.smile, b.smile) && near(a.eye_happy, b.eye_happy) && near(a.glow_rgb[0], b.glow_rgb[0]));
    av_pose_t c = av_model_step(&m, 100 + AV_BLEND_MS), d = av_mood_base(AV_SPEAK);
    CHECK(near(c.smile, d.smile) && near(c.eye_happy, d.eye_happy) && near(c.glow_rgb[1], d.glow_rgb[1]));
}
static void muted_closes_red_eyes(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_mood(&m, AV_MUTED, 0);
    av_pose_t p = av_model_step(&m, AV_BLEND_MS);
    CHECK(near(p.red, 1.0f)); CHECK(p.eye_open < 0.05f);
    av_model_step(&m, m.next_blink + AV_BLINK_MS / 2);
    CHECK(!m.blinking);                                      /* closed eyes do not blink */
    av_model_greet(&m, 9000);
    CHECK(!m.greeting);                                      /* and a muted Nova does not hop */
}
static void greet_hops_then_ends(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_greet(&m, 500);
    av_pose_t p = av_model_step(&m, 500 + AV_GREET_MS / 2);
    CHECK(p.eye_happy > 0.9f); CHECK(p.smile > 0.9f);
    CHECK(av_model_animating(&m, 500 + AV_GREET_MS / 2));
    av_model_step(&m, 500 + AV_GREET_MS + 1);
    CHECK(!m.greeting);
}
static void speaking_mouth_follows_level(void) {
    av_model_t m; av_model_init(&m, 7, 0);
    av_model_set_mood(&m, AV_SPEAK, 0);
    av_model_set_level(&m, 255);
    CHECK(near(av_model_step(&m, AV_BLEND_MS).mouth_open, 1.0f));
    av_model_set_level(&m, 0);                               /* no level: a synthesised mouth that moves */
    float a = av_model_step(&m, 1000).mouth_open, b = av_model_step(&m, 1140).mouth_open;
    CHECK(fabsf(a - b) > 0.05f);
    CHECK(av_model_animating(&m, 1200));
    av_model_set_mood(&m, AV_IDLE, 2000);
    CHECK(av_model_step(&m, 2000 + AV_BLEND_MS).mouth_open < 0.01f);
}
static void mood_mapping(void) {
    CHECK_EQ(av_mood_for(NV_IDLE, false), AV_IDLE);
    CHECK_EQ(av_mood_for(NV_LISTENING, false), AV_LISTEN);
    CHECK_EQ(av_mood_for(NV_THINKING, false), AV_THINK);
    CHECK_EQ(av_mood_for(NV_SPEAKING, false), AV_SPEAK);
    CHECK_EQ(av_mood_for(NV_SPEAKING, true), AV_MUTED);
}
static void pose_stays_in_range(void) {
    for(int mood = 0; mood < AV_MOOD_COUNT; mood++) {
        av_model_t m; av_model_init(&m, 3, 0);
        av_model_set_mood(&m, (av_mood_t)mood, 0);
        av_model_greet(&m, 0);
        for(uint32_t t = 0; t < 12000; t += 37) {
            av_pose_t p = av_model_step(&m, t);
            CHECK(p.eye_open >= 0.0f && p.eye_open <= 1.0f);
            CHECK(p.eye_happy >= 0.0f && p.eye_happy <= 1.0f);
            CHECK(p.smile >= 0.0f && p.smile <= 1.0f);
            CHECK(p.mouth_open >= 0.0f && p.mouth_open <= 1.0f);
            CHECK(p.glow >= 0.0f && p.glow <= 1.0f);
            CHECK(p.red >= 0.0f && p.red <= 1.0f);
            CHECK(fabsf(p.look_x) <= 1.0f && fabsf(p.look_y) <= 1.0f);
            CHECK(fabsf(p.bob) <= 0.1f && fabsf(p.tilt) <= 0.25f);
        }
    }
}
int main(void) {
    RUN(starts_idle_and_still); RUN(blinks_close_then_reopen); RUN(mood_cross_fades); RUN(muted_closes_red_eyes);
    RUN(greet_hops_then_ends); RUN(speaking_mouth_follows_level); RUN(mood_mapping); RUN(pose_stays_in_range);
    DONE();
}
