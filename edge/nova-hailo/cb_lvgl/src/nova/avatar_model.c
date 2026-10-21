#include "nova/avatar_model.h"
#include <string.h>

#define PI 3.1415927f
#define POSE_FIELDS (sizeof(av_pose_t) / sizeof(float))

_Static_assert(sizeof(av_pose_t) == 14 * sizeof(float), "av_pose_t is 14 floats");

static bool due(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }
static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

static uint32_t rnd(uint32_t *s)
{
    *s = *s * 1664525u + 1013904223u;
    return *s >> 8;
}

static uint32_t blink_gap(av_model_t *m) { return 3000u + rnd(&m->seed) % 3001u; }   /* 3 .. 6 s */

static const av_pose_t k_rest = { .eyeW = 0.15f, .eyeH = 0.32f, .lidT = 0.08f, .hue = 196.0f, .sat = 40.0f, .bob = 1.0f };

av_pose_t av_state_base(av_state_t s)
{
    av_pose_t p = k_rest;
    switch(s) {
        case AV_ST_GREETING: p.happy = 1.0f; p.lidT = 0.0f; p.hue = 26.0f; p.sat = 50.0f; p.spark = 1.0f; p.bob = 2.4f; break;
        case AV_ST_LISTENING: p.eyeW = 0.165f; p.eyeH = 0.32f; p.hue = 202.0f; p.sat = 55.0f; p.lean = -0.05f; p.lidT = 0.08f;
                              p.lookX = -0.03f; break;
        case AV_ST_THINKING: p.eyeH = 0.24f; p.lidT = 0.0f; p.lookY = -0.09f; p.lidB = 0.14f; p.hue = 258.0f; p.sat = 34.0f;
                             p.bob = 0.5f; break;
        case AV_ST_SPEAKING: p.eyeH = 0.30f; p.happy = 0.15f; p.lidT = 0.10f; p.hue = 164.0f; p.sat = 40.0f; break;
        case AV_ST_MUTED: p.eyeW = 0.18f; p.eyeH = 0.045f; p.lidT = 0.0f; p.hue = 214.0f; p.sat = 5.0f; p.bob = 0.3f; break;
        default: break;
    }
    return p;
}

static av_pose_t personality(av_mood_t mood)
{
    (void)mood;
    /* TODO(user): each mood's resting offsets: additive happy, asym, tilt, lookX, lookY, lidT, lidB; replaced eyeW, eyeH, hue, sat, bob (AV_KEEP keeps the state's value). */
    return (av_pose_t){ .eyeW = AV_KEEP, .eyeH = AV_KEEP, .hue = AV_KEEP, .sat = AV_KEEP, .bob = AV_KEEP };
}

av_pose_t av_mood_delta(av_mood_t mood) { return personality(mood); }

static float replaced(float base, float d) { return isnan(d) ? base : d; }

av_pose_t av_model_target_with(const av_model_t *m, av_pose_t d)
{
    av_pose_t b = av_state_base(m->state), t = b;
    bool muted = m->state == AV_ST_MUTED;
    t.happy = clampf(b.happy + d.happy, 0.0f, 1.0f);
    t.lidT = clampf(b.lidT + d.lidT, 0.0f, 1.0f);
    t.lidB = clampf(b.lidB + d.lidB, 0.0f, 1.0f);
    t.asym = b.asym + d.asym;
    t.tilt = b.tilt + d.tilt;
    t.lookX = b.lookX + d.lookX;
    t.lookY = b.lookY + d.lookY;
    if(!muted) {   /* a muted face keeps its own size and colour */
        t.eyeW = replaced(b.eyeW, d.eyeW);
        t.eyeH = replaced(b.eyeH, d.eyeH);
        t.hue = replaced(b.hue, d.hue);
        t.sat = replaced(b.sat, d.sat);
        t.bob = replaced(b.bob, d.bob);
    }
    else t.happy = t.asym = t.lidT = 0.0f;
    return t;
}

av_pose_t av_model_target(const av_model_t *m) { return av_model_target_with(m, av_mood_delta(m->mood)); }

static float scan_target(const av_model_t *m) { return m->state == AV_ST_THINKING ? 1.0f : 0.0f; }

void av_model_init(av_model_t *m, uint32_t seed, uint32_t now)
{
    *m = (av_model_t){ .state = AV_ST_IDLE, .before_greet = AV_ST_IDLE, .mood = AV_CALM, .seed = seed ? seed : 1u,
                       .last_ms = now, .duck = 1.0f };
    m->pose = av_model_target(m);
    m->next_blink = now + blink_gap(m);
}

void av_model_set_state(av_model_t *m, av_state_t s, uint32_t now)
{
    if(s >= AV_STATE_COUNT) return;
    if(s == AV_ST_GREETING) {
        if(m->state == AV_ST_MUTED) return;
        if(m->state != AV_ST_GREETING) m->before_greet = m->state;
        m->state = s;
        m->greeting = true;
        m->greet_t0 = now;
        return;
    }
    m->greeting = false;
    m->state = s;
    if(s == AV_ST_MUTED) m->blinking = false;
}

void av_model_set_mood(av_model_t *m, av_mood_t mood)
{
    if(mood < AV_MOOD_COUNT) m->mood = mood;
}

void av_model_set_level(av_model_t *m, bool nova, float level)
{
    level = clampf(level, 0.0f, 1.0f);
    if(nova) m->nova = level;
    else m->driver = level;
}

void av_model_set_reduce_motion(av_model_t *m, bool on)
{
    m->reduce_motion = on;
    if(on) m->blinking = false;
}

void av_model_set_duck(av_model_t *m, float gain) { m->duck = clampf(gain, 0.0f, 1.0f); }

/* Signed shortest way round the colour wheel from a to b, in degrees. */
static float hue_diff(float a, float b) { return fmodf(b - a + 540.0f, 360.0f) - 180.0f; }

/* The voice envelope when no level arrives: two beating sines plus a fast one, 0.25 .. 1. */
static float envelope(float t, float seed)
{
    float s = sinf(t * 9.3f + seed) * sinf(t * 3.1f + seed * 2.0f) + 0.35f * sinf(t * 17.7f + seed);
    return clampf(0.25f + 0.75f * fabsf(s), 0.0f, 1.0f);
}

av_frame_t av_model_step(av_model_t *m, uint32_t now)
{
    int32_t dt = (int32_t)(now - m->last_ms);
    m->last_ms = now;
    if(m->greeting && now - m->greet_t0 >= AV_GREET_MS) {
        m->greeting = false;
        m->state = m->before_greet;
    }

    float k = dt > 0 ? 1.0f - expf(-(float)dt / (float)AV_TAU_MS) : 0.0f;
    av_pose_t tgt = av_model_target(m);
    float *p = (float *)&m->pose;
    const float *g = (const float *)&tgt;
    if(m->reduce_motion) k = 1.0f;
    for(size_t i = 0; i < POSE_FIELDS; i++) {
        if(&p[i] == &m->pose.hue) {
            float h = p[i] + hue_diff(p[i], g[i]) * k;
            p[i] = h < 0.0f ? h + 360.0f : h >= 360.0f ? h - 360.0f : h;
        }
        else p[i] += (g[i] - p[i]) * k;
    }
    m->scan += (scan_target(m) - m->scan) * k;

    float t = (float)(now % 600000u) / 1000.0f;   /* phases repeat every 10 min, so floats stay exact */
    av_frame_t f = { .p = m->pose, .swell = 1.0f, .nod = 1.0f };

    if(m->reduce_motion) {
        if(m->state == AV_ST_SPEAKING) f.p.eyeH *= 1.0f - 0.12f * (m->nova > 0.0f ? m->nova : 0.5f) * m->duck;
        f.hop = 0.0f;
        if(due(now, m->next_blink)) m->next_blink = now + blink_gap(m);   /* no blinks: keep the wake time ahead */
        return f;
    }
    if(m->state == AV_ST_LISTENING) {
        float driver = m->driver > 0.0f ? m->driver : envelope(t * 0.85f, 2.1f);
        f.swell = 1.0f + 0.025f * driver;
    }
    if(m->state == AV_ST_SPEAKING) {
        float nova = (m->nova > 0.0f ? m->nova : envelope(t, 0.3f)) * m->duck;
        f.p.eyeH *= 1.0f - 0.12f * nova;
        f.nod = 1.0f - 0.02f * nova;
    }
    f.p.lookX += 0.08f * sinf(0.9f * t) * m->scan;
    f.hop = 0.03f * m->pose.bob * sinf(t * (m->state == AV_ST_GREETING ? 5.0f : 1.4f));

    if(due(now, m->next_blink)) {
        if(m->state == AV_ST_MUTED) m->next_blink = now + blink_gap(m);
        else m->blinking = true;
    }
    if(m->blinking) {
        float q = (float)(now - m->next_blink) / (float)AV_BLINK_MS;
        if(q >= 1.0f) {
            m->blinking = false;
            m->next_blink = now + blink_gap(m);
        }
        else f.p.lidT = fmaxf(f.p.lidT, sinf(PI * q));
    }
    f.p.lidT = clampf(f.p.lidT, 0.0f, 1.0f);
    return f;
}

bool av_model_settled(const av_model_t *m)
{
    if(m->greeting || m->blinking) return false;
    if(!m->reduce_motion &&
       (m->state == AV_ST_LISTENING || m->state == AV_ST_THINKING || m->state == AV_ST_SPEAKING)) return false;
    av_pose_t tgt = av_model_target(m);
    const float *p = (const float *)&m->pose, *g = (const float *)&tgt;
    const float *hue = &m->pose.hue, *sat = &m->pose.sat;
    for(size_t i = 0; i < POSE_FIELDS; i++) {
        float eps = (&p[i] == hue || &p[i] == sat) ? 0.5f : 0.002f;
        float d = &p[i] == hue ? hue_diff(p[i], g[i]) : g[i] - p[i];
        if(fabsf(d) > eps) return false;
    }
    return fabsf(scan_target(m) - m->scan) <= 0.002f;
}

uint32_t av_model_next_wake(const av_model_t *m) { return m->next_blink; }

av_eye_t av_eye_morph(float eyeH, float happy)
{
    av_eye_t e;
    e.pill_h = eyeH * (1.0f - 0.9f * clampf((happy - 0.2f) / 0.25f, 0.0f, 1.0f));
    e.pill_opa = happy < 0.45f ? 1.0f : 0.0f;
    e.arch_opa = 1.0f - e.pill_opa;
    e.arch_rise = clampf((happy - 0.45f) / 0.2f, 0.0f, 1.0f);
    return e;
}

av_state_t av_state_for(nv_activity_t a, bool muted)
{
    if(muted) return AV_ST_MUTED;
    switch(a) {
        case NV_LISTENING: return AV_ST_LISTENING;
        case NV_THINKING: return AV_ST_THINKING;
        case NV_SPEAKING: return AV_ST_SPEAKING;
        default: return AV_ST_IDLE;
    }
}

static const char *const k_head_id[AV_HEAD_COUNT] = { "pebble", "capsule", "dome", "drop", "cloud", "gem", "visor", "eyes" };

av_head_t av_head_for_voice(const char *voice_id)
{
    static const struct { const char *voice; av_head_t head; } k_map[] = {
        { "en_US-amy-low", AV_HEAD_PEBBLE },        { "en_US-lessac-medium", AV_HEAD_DROP },
        { "en_US-ryan-high", AV_HEAD_GEM },         { "en_GB-alba-medium", AV_HEAD_CLOUD },
        { "en_US-hfc_female-medium", AV_HEAD_CAPSULE },
    };
    if(voice_id)
        for(size_t i = 0; i < sizeof k_map / sizeof k_map[0]; i++)
            if(!strcmp(voice_id, k_map[i].voice)) return k_map[i].head;
    return AV_HEAD_PEBBLE;
}

av_head_t av_head_parse(const char *id)
{
    if(id)
        for(int h = 0; h < AV_HEAD_COUNT; h++)
            if(!strcmp(id, k_head_id[h])) return (av_head_t)h;
    return AV_HEAD_COUNT;
}

const char *av_head_id(av_head_t h) { return h < AV_HEAD_COUNT ? k_head_id[h] : "pebble"; }
