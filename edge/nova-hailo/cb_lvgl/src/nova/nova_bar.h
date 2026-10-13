#pragma once
#include "cb_theme.h"
#include "lvgl.h"
#include "nova/nova_fsm.h"

/* The Nova bar: orb + state + transcript + mic/end/tuck. OPEN = full 760x72 pill; REST = Figma frame C's
   cluster (orb with mic stacked over end), centred where the bar was. It only draws: every decision comes
   from the nova_fsm it is given; taps are fed back into that FSM and the effects reported via on_fx. */

typedef struct nova_bar nova_bar_t;
typedef void (*nova_bar_fx_cb_t)(unsigned fx, void *user_data);

nova_bar_t *nova_bar_create(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data);
void nova_bar_apply(nova_bar_t *b, unsigned fx);                     /* reflect FSM state after an event */
void nova_bar_set_text(nova_bar_t *b, const char *text, bool user);  /* typewriter, 2 chars / 26 ms */
void nova_bar_stream_text(nova_bar_t *b, const char *text);          /* Nova's reply growing: keeps typing on */
lv_obj_t *nova_bar_obj(nova_bar_t *b);
lv_obj_t *nova_bar_orb(nova_bar_t *b);
lv_obj_t *nova_bar_text_col(nova_bar_t *b);                          /* state line + transcript */
bool nova_bar_is_animating(const nova_bar_t *b);
void nova_bar_set_reduce_motion(nova_bar_t *b, bool on);
void nova_bar_set_pending(nova_bar_t *b, bool pending);              /* live: mic / end dim while a request runs */

/* Shared with the v2 Nova panel, so both move and look alike. */
float nova_bar_spring(float t_s, bool expand);                       /* 0..1 damped spring, t in seconds */
lv_obj_t *nova_bar_round_button(lv_obj_t *parent, int32_t size, lv_color_t bg, lv_opa_t opa);   /* with press motion */
void nova_bar_mic_glyph(lv_obj_t *btn);
lv_obj_t *nova_bar_glyph(lv_obj_t *btn, const char *txt, cb_color_t c);
