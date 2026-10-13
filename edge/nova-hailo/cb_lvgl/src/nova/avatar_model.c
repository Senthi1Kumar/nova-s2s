#include "nova/avatar_model.h"
#include <math.h>

#define TAU 6.2831853f
#define PI 3.1415927f

static bool due(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }
static float lerpf(float a, float b, float t) { return a + (b - a) * t; }
static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

static uint32_t rnd(uint32_t *s)
{
    *s = *s * 1664525u + 1013904223u;
    return *s >> 8;
}

static uint32_t blink_gap(av_model_t *m) { return 2600u + rnd(&m->seed) % 3400u; }   /* 2.6 .. 6 s */

/* Nova's personality: the resting face of each awake mood. Muted and the mood colours are fixed in
   av_mood_base(); blinks, the greeting, the thinking glance and the speaking mouth are layered on in
   av_model_step(). */
static av_pose_t personality(av_mood_t mood)
{
    av_pose_t p = { .eye_open = 1.0f, .smile = 0.45f, .glow = 0.3f };
    /* TODO(user): give each mood its own resting face. */
    (void)mood;
    return p;
}

av_pose_t av_mood_base(av_mood_t mood)
{
    static const uint32_t hue[AV_MOOD_COUNT] = { 0x6fb8d8, 0x4cd6ff, 0xb66bff, 0x4dffb4, 0xff5a6a };
    av_pose_t p = mood == AV_MUTED ? (av_pose_t){ .smile = 0.2f, .glow = 0.15f, .red = 1.0f } : personality(mood);
    uint32_t h = hue[mood < AV_MOOD_COUNT ? mood : AV_IDLE];
    p.glow_rgb[0] = (float)((h >> 16) & 0xff) / 255.0f;
    p.glow_rgb[1] = (float)((h >> 8) & 0xff) / 255.0f;
    p.glow_rgb[2] = (float)(h & 0xff) / 255.0f;
    return p;
}

static av_pose_t pose_mix(const av_pose_t *a, const av_pose_t *b, float t)
{
    av_pose_t p;
    p.eye_open = lerpf(a->eye_open, b->eye_open, t);
    p.eye_happy = lerpf(a->eye_happy, b->eye_happy, t);
    p.look_x = lerpf(a->look_x, b->look_x, t);
    p.look_y = lerpf(a->look_y, b->look_y, t);
    p.mouth_open = lerpf(a->mouth_open, b->mouth_open, t);
    p.smile = lerpf(a->smile, b->smile, t);
    p.glow = lerpf(a->glow, b->glow, t);
    for(int i = 0; i < 3; i++) p.glow_rgb[i] = lerpf(a->glow_rgb[i], b->glow_rgb[i], t);
    p.bob = lerpf(a->bob, b->bob, t);
    p.tilt = lerpf(a->tilt, b->tilt, t);
    p.red = lerpf(a->red, b->red, t);
    return p;
}

void av_model_init(av_model_t *m, uint32_t seed, uint32_t now)
{
    *m = (av_model_t){ .mood = AV_IDLE, .from = AV_IDLE, .mood_t0 = now - AV_BLEND_MS, .seed = seed ? seed : 1u };
    m->next_blink = now + blink_gap(m);
}

void av_model_set_mood(av_model_t *m, av_mood_t mood, uint32_t now)
{
    if(m->mood == mood) return;
    m->from = m->mood;
    m->mood = mood;
    m->mood_t0 = now;
    if(mood == AV_MUTED) m->greeting = m->blinking = false;
}

void av_model_greet(av_model_t *m, uint32_t now)
{
    if(m->mood == AV_MUTED) return;
    m->greeting = true;
    m->greet_t0 = now;
}

void av_model_set_level(av_model_t *m, uint8_t level) { m->level = level; }

static float blend_k(const av_model_t *m, uint32_t now)
{
    uint32_t dt = now - m->mood_t0;
    if(dt >= AV_BLEND_MS) return 1.0f;
    float k = (float)dt / AV_BLEND_MS;
    return k * k * (3.0f - 2.0f * k);
}

av_pose_t av_model_step(av_model_t *m, uint32_t now)
{
    float k = blend_k(m, now);
    av_pose_t a = av_mood_base(m->from), b = av_mood_base(m->mood);
    av_pose_t p = pose_mix(&a, &b, k);
    float t = (float)(now % 600000u) / 1000.0f;   /* phases repeat every 10 min, so floats stay exact */

    p.bob += 0.012f * sinf(TAU * t / 3.2f);   /* breathing */
    switch(m->mood) {
        case AV_LISTEN:   /* leans in, the halo pulses */
            p.tilt += k * 0.06f;
            p.glow += k * 0.12f * sinf(TAU * t / 1.4f);
            break;
        case AV_THINK:    /* the eyes wander side to side */
            p.look_x += k * 0.35f * sinf(TAU * t / 2.4f);
            break;
        case AV_SPEAK: {
            float open = m->level ? (float)m->level / 255.0f : 0.25f + 0.55f * fabsf(sinf(TAU * t / 0.56f));
            p.mouth_open = lerpf(p.mouth_open, open, k);
            p.bob += k * 0.008f * open;
            break;
        }
        default: break;
    }

    if(m->mood != AV_MUTED) {
        if(!m->blinking && due(now, m->next_blink)) {
            m->blinking = true;
            m->blink_t0 = now;
        }
        if(m->blinking) {
            float q = (float)(now - m->blink_t0) / AV_BLINK_MS;
            if(q >= 1.0f) {
                m->blinking = false;
                m->next_blink = now + blink_gap(m);
            }
            else p.eye_open *= fabsf(1.0f - 2.0f * q);   /* shut at the midpoint */
        }
    }

    if(m->greeting) {
        float q = (float)(now - m->greet_t0) / AV_GREET_MS;
        if(q >= 1.0f) m->greeting = false;
        else {
            float env = q < 0.15f ? q / 0.15f : q > 0.75f ? (1.0f - q) / 0.25f : 1.0f;
            p.eye_happy = lerpf(p.eye_happy, 1.0f, env);
            p.smile = lerpf(p.smile, 1.0f, env);
            p.glow = lerpf(p.glow, 1.0f, 0.6f * env);
            p.bob -= 0.06f * fabsf(sinf(2.0f * PI * q)) * (1.0f - q);   /* two hops, the second smaller */
            p.tilt += 0.10f * sinf(2.0f * TAU * q) * (1.0f - q);
        }
    }

    p.eye_open = clampf(p.eye_open, 0.0f, 1.0f);
    p.eye_happy = clampf(p.eye_happy, 0.0f, 1.0f);
    p.look_x = clampf(p.look_x, -1.0f, 1.0f);
    p.look_y = clampf(p.look_y, -1.0f, 1.0f);
    p.mouth_open = clampf(p.mouth_open, 0.0f, 1.0f);
    p.smile = clampf(p.smile, 0.0f, 1.0f);
    p.glow = clampf(p.glow, 0.0f, 1.0f);
    p.red = clampf(p.red, 0.0f, 1.0f);
    p.bob = clampf(p.bob, -0.1f, 0.1f);
    p.tilt = clampf(p.tilt, -0.25f, 0.25f);
    return p;
}

bool av_model_animating(const av_model_t *m, uint32_t now)
{
    return m->greeting || m->blinking || now - m->mood_t0 < AV_BLEND_MS || m->mood == AV_LISTEN ||
           m->mood == AV_THINK || m->mood == AV_SPEAK;
}

av_mood_t av_mood_for(nv_activity_t a, bool muted)
{
    if(muted) return AV_MUTED;
    switch(a) {
        case NV_LISTENING: return AV_LISTEN;
        case NV_THINKING: return AV_THINK;
        case NV_SPEAKING: return AV_SPEAK;
        default: return AV_IDLE;
    }
}
