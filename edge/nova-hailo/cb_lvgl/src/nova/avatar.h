#pragma once
#include "lvgl.h"
#include "nova/nova_fsm.h"

/* Nova's avatar for layout v2: a tiny round robot head (white shell, black visor, cyan eyes, side pods, antenna
   light) drawn per pixel from an av_pose_t (avatar_model.h) into one ARGB image, with anti-aliased edges and a
   halo in the mood's colour. ~30 fps while listening, thinking, speaking, blinking or greeting; the shared
   ambient clock otherwise; still under reduce-motion. The returned object is face x face and clickable; the
   halo, hops and antenna draw outside it. */
lv_obj_t *avatar_create(lv_obj_t *parent, int32_t face);
void avatar_set_state(lv_obj_t *av, nv_activity_t a, bool muted);
void avatar_set_level(lv_obj_t *av, uint8_t level);   /* speaking amplitude 0..255; 0 = synthesised */
void avatar_greet(lv_obj_t *av);                      /* "Hi": happy eyes and a double hop */
