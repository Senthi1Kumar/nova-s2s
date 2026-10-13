#pragma once
#include "lvgl.h"
#include "cb_demo.h"
#include "nova/nova_bar.h"
#include "nova/nova_fsm.h"
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
nova_bar_t *cb_app_bar(void);       /* NULL in layout v2 */
nova_panel_t *cb_app_panel(void);   /* layout v2 only */
cb_demo_t *cb_app_demo(void);

/* Nova's on-screen presence, whichever layout shows it (the bar, or the v2 panel). Safe before build. */
void cb_app_nova_apply(unsigned fx);
void cb_app_nova_text(const char *text, bool user);
void cb_app_nova_stream(const char *text);
void cb_app_nova_pending(bool pending);
void cb_app_nova_card(const nova_card_view_t *card);   /* v2: the panel's table; the bar has none */
void cb_app_nova_greet(void);                          /* v2: the avatar says hi; the bar has none */
