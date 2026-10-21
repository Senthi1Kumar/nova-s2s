#pragma once
#include "lvgl.h"
#include "nova/avatar_model.h"
#include "nova/nova_fsm.h"

/* Nova's face: eyes on one of eight heads (the "NovaBot Faces" look), built from a handful of LVGL objects and
   driven by av_model_t. Everything lives inside one box x box object, so a frame invalidates only the box.
   The face radius is 0.27 * box; the head sits a little below the middle, with room above for the hop and the
   greeting sparkles. A 33 ms timer runs while anything moves and pauses when the model settles; a one-shot
   timer wakes it for the next blink (never under reduce-motion). Any avatar_set_* call resumes it. */
lv_obj_t *avatar_create(lv_obj_t *parent, int32_t box);
void avatar_set_state(lv_obj_t *av, nv_activity_t a, bool muted);
void avatar_set_av_state(lv_obj_t *av, av_state_t s);          /* a state by its face-model name (snapshots) */
void avatar_set_mood(lv_obj_t *av, av_mood_t mood);
void avatar_set_head(lv_obj_t *av, av_head_t head);
void avatar_set_level(lv_obj_t *av, bool nova, uint8_t level);   /* 0..255; 0 = synthesised */
void avatar_set_duck(lv_obj_t *av, float gain);
void avatar_greet(lv_obj_t *av);                               /* "Hi": happy eyes, a hop, two sparkles */
bool avatar_timer_running(lv_obj_t *av);                       /* the 33 ms timer is live */

/* Avatars created from now on start with this head. */
void avatar_set_default_head(av_head_t head);
/* Avatars created from now on start with this mood (cb_app keeps it current). */
void avatar_set_default_mood(av_mood_t mood);
/* Every live avatar (the v2 panel and the layout B dock), for pushes that are not aimed at one. */
void avatar_for_each(void (*fn)(lv_obj_t *av, void *ud), void *ud);
