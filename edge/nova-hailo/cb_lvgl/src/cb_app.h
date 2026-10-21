#pragma once
#include "lvgl.h"
#include "cb_demo.h"
#include "nova/nova_bar.h"
#include "nova/avatar_model.h"
#include "nova/nova_fsm.h"
#include "nova/nova_mood.h"
#include "nova/nova_panel.h"

/* Builds and wires the whole app into a 1920x720 panel: model + sim, (dashboard), Nova bar, demo,
   and a 50 ms timer that ticks the FSM and the demo. Shared by main and the headless smoke test. */
typedef enum {
    CB_LAYOUT_D,    /* frame D: 1920x720 columns */
    CB_LAYOUT_B,    /* B: car + pages, full screen, the Nova bar in the dock */
    CB_LAYOUT_V2    /* v0.0.2: B's pages with the avatar Nova panel over the device tiles */
} cb_layout_t;

typedef struct {
    bool demo;
    bool reduce_motion;
    cb_layout_t layout;
    const char *live_url;   /* ws://…/v1/observe: the pipeline drives the cabin (no sim, no demo) */
    const char *replay;     /* JSONL recording replayed instead of a live link */
    double replay_speed;
} cb_app_cfg_t;

void cb_app_build(lv_obj_t *panel, const cb_app_cfg_t *cfg);
unsigned cb_app_event(nv_event_t ev, const nv_done_t *done);   /* FSM + bar + demo abort; keys use this */
void cb_app_toggle_demo_pause(void);
nova_fsm_t *cb_app_fsm(void);
nova_bar_t *cb_app_bar(void);       /* layout D only */
nova_panel_t *cb_app_panel(void);   /* layouts B (dock) and v2 */
cb_demo_t *cb_app_demo(void);

/* Nova's on-screen presence, whichever layout shows it (the D bar, or the panel in B and v2). Safe before build. */
void cb_app_nova_apply(unsigned fx);
void cb_app_nova_text(const char *text, bool user);
void cb_app_nova_stream(const char *text);
void cb_app_nova_pending(bool pending);
void cb_app_nova_card(const nova_card_view_t *card);   /* v2 panel or layout B dock */
void cb_app_nova_greet(void);                          /* the avatar says hi; the bar has none */
void cb_app_nova_route_line(const char *line);         /* B: the dock's instruction line; NULL when the route ends */
void cb_app_nova_guidance(const char *text);           /* B: a spoken route prompt, not an answer */

/* Nova's mood (the face's expression): events feed a tracker the 50 ms tick reads, and a change reaches every
   live avatar. */
void cb_app_nova_mood_event(nm_event_t ev);
void cb_app_nova_duck(float gain);                     /* the faces turn down with the voice */
/* The head every avatar wears: the settings' avatar id if it names one, else the voice's own head
   (NULL or empty: Pebble). Avatars made later start with it. */
void cb_app_set_avatar(const char *avatar_setting, const char *voice_id);
