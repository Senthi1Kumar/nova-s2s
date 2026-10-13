#pragma once
#include "nova/nova_fsm.h"
#include <stdbool.h>
#include <stdint.h>

/* Nova's face as numbers, after the Muse gadget SDK's muse_pixel (a small pose struct {mode, t, level, happy}
   drives a procedural face). Pure C so the face can be tested: avatar.c draws an av_pose_t, this decides it.
   Moods cross-fade over AV_BLEND_MS; blinks, the greeting hop, the thinking glance and the speaking mouth are
   layered on top of the mood's resting pose. */
typedef enum { AV_IDLE, AV_LISTEN, AV_THINK, AV_SPEAK, AV_MUTED, AV_MOOD_COUNT } av_mood_t;

typedef struct {
    float eye_open;         /* 0 closed .. 1 open */
    float eye_happy;        /* 0 round eyes .. 1 "^^" arcs */
    float look_x, look_y;   /* gaze, -1..1 (+ = right, down) */
    float mouth_open;       /* 0 closed .. 1 open */
    float smile;            /* 0 flat .. 1 wide */
    float glow;             /* halo strength 0..1 */
    float glow_rgb[3];      /* halo colour 0..1: the mood's colour, as on the orb */
    float bob;              /* vertical offset in face heights (+ = down) */
    float tilt;             /* head tilt in radians (+ = clockwise) */
    float red;              /* 0..1: the eyes turn red (muted) */
} av_pose_t;

#define AV_BLEND_MS 320u
#define AV_BLINK_MS 160u
#define AV_GREET_MS 1600u

typedef struct {
    av_mood_t mood, from;
    uint32_t mood_t0;
    uint32_t next_blink, blink_t0;
    bool blinking;
    uint32_t greet_t0;
    bool greeting;
    uint32_t seed;
    uint8_t level;   /* speaking amplitude 0..255; 0 = a synthesised mouth */
} av_model_t;

void av_model_init(av_model_t *m, uint32_t seed, uint32_t now);
void av_model_set_mood(av_model_t *m, av_mood_t mood, uint32_t now);
void av_model_greet(av_model_t *m, uint32_t now);              /* "Hi": a happy double hop, AV_GREET_MS */
void av_model_set_level(av_model_t *m, uint8_t level);
av_pose_t av_model_step(av_model_t *m, uint32_t now);           /* advances blinks and the greeting */
bool av_model_animating(const av_model_t *m, uint32_t now);    /* wants ~30 fps; otherwise the ambient clock */
av_mood_t av_mood_for(nv_activity_t a, bool muted);
av_pose_t av_mood_base(av_mood_t mood);                         /* the mood's resting face */
