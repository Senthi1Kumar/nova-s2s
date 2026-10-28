#pragma once
#include "nova/nova_fsm.h"
#include <math.h>
#include <stdbool.h>
#include <stdint.h>

/* Nova's face as numbers (the "NovaBot Faces" model: eyes only). Pure C so it can be tested: the renderer draws
   an av_frame_t, this decides it. A state (idle, listening ...) gives the resting pose, the mood adds a small
   offset on top, and the pose eases toward that target; blinks, the scan, the greeting hop and the live voice
   levels are layered on in av_model_step(). Sizes are fractions of the head box; hue is degrees, sat percent. */
typedef enum { AV_ST_IDLE, AV_ST_GREETING, AV_ST_LISTENING, AV_ST_THINKING, AV_ST_SPEAKING, AV_ST_MUTED,
               AV_STATE_COUNT } av_state_t;
typedef enum { AV_CALM, AV_HAPPY, AV_CURIOUS, AV_CONCERNED, AV_SURPRISED, AV_SLEEPY, AV_MOOD_COUNT } av_mood_t;
typedef enum { AV_HEAD_PEBBLE, AV_HEAD_CAPSULE, AV_HEAD_DOME, AV_HEAD_DROP, AV_HEAD_CLOUD, AV_HEAD_GEM,
               AV_HEAD_VISOR, AV_HEAD_EYES, AV_HEAD_COUNT } av_head_t;

typedef struct { float eyeW, eyeH, lidT, lidB, happy, lookX, lookY, tilt, asym, lean, hue, sat, spark, bob; } av_pose_t;
typedef struct { av_pose_t p; float swell, nod, hop; } av_frame_t;   /* what avatar.c draws */
typedef struct { float pill_h, pill_opa, arch_opa, arch_rise; } av_eye_t;

#define AV_KEEP NAN   /* in a mood delta: keep the state's size/colour */
#define AV_TAU_MS 140u
#define AV_BLINK_MS 160u
#define AV_GREET_MS 1600u

typedef struct {
    av_state_t state, before_greet;
    av_mood_t mood;
    av_pose_t pose;
    float scan;                       /* 0..1, eased: the thinking glance */
    uint32_t next_blink, greet_t0, last_ms, seed;
    bool blinking, greeting, reduce_motion;
    float driver, nova, duck;         /* levels 0..1; 0 = synthesised envelope; duck scales Nova's */
} av_model_t;

void av_model_init(av_model_t *m, uint32_t seed, uint32_t now);
void av_model_set_state(av_model_t *m, av_state_t s, uint32_t now);   /* GREETING lasts AV_GREET_MS, then back */
void av_model_set_mood(av_model_t *m, av_mood_t mood);
void av_model_set_level(av_model_t *m, bool nova, float level);       /* 0..1; 0 = synthesised envelope */
void av_model_set_reduce_motion(av_model_t *m, bool on);              /* snap to the target; no blink, hop, scan */
void av_model_set_duck(av_model_t *m, float gain);                    /* scales Nova's level */
av_frame_t av_model_step(av_model_t *m, uint32_t now);
av_pose_t av_state_base(av_state_t s);                                /* the state's resting pose */
av_pose_t av_model_target(const av_model_t *m);                       /* state base + mood delta, clamped */
av_pose_t av_model_target_with(const av_model_t *m, av_pose_t delta);   /* the target for a given mood delta */
av_pose_t av_mood_delta(av_mood_t mood);                              /* personality() */
bool av_model_settled(const av_model_t *m);                           /* nothing but the idle breathing moves */
uint32_t av_model_next_wake(const av_model_t *m);                     /* the next blink, ms */
av_eye_t av_eye_morph(float eyeH, float happy);
av_state_t av_state_for(nv_activity_t a, bool muted);
av_head_t av_head_for_voice(const char *voice_id);
av_head_t av_head_parse(const char *id);                              /* "voice"/unknown -> AV_HEAD_COUNT */
const char *av_head_id(av_head_t h);                                  /* "pebble" ... "eyes" */
