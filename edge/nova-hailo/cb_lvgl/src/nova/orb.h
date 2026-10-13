#pragma once
#include "lvgl.h"
#include "nova/nova_fsm.h"

/* Nova's orb: a small sphere rendered per pixel into one image (body, glow and highlight together, identical on
   both renderers). Its white highlight and three soft colour spots from the state's palette swirl slowly around
   the sphere; thinking swirls faster, speaking breathes, a wake blooms the glow. ~30 fps while Nova is active,
   the shared ambient clock while idle, static under reduce-motion. The returned object is clickable. */
lv_obj_t *orb_create(lv_obj_t *parent, int32_t diameter);
void orb_set_state(lv_obj_t *orb, nv_activity_t a, bool muted);   /* palettes cross-fade over 450 ms */
void orb_set_level(lv_obj_t *orb, uint8_t level);                  /* speaking amplitude 0..255; 0 = synthesised */
void orb_bloom(lv_obj_t *orb);                                     /* CHIME: the glow swells and settles (700 ms) */
