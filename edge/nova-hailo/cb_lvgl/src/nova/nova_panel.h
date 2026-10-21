#pragma once
#include "lvgl.h"
#include "nova/nova_bar.h"
#include "nova/nova_fsm.h"

/* Layout v2's Nova: a glass panel with the avatar, the state, the user's words (a marquee when they do not fit)
   and Nova's reply (three lines), an answer card as a small table, and mic / end / tuck. REST is the avatar row
   alone; OPEN adds the reply and the card; the height springs like the bar's width. A green dot on the avatar's
   corner marks a notification (an answer card that arrived while tucked) until the panel opens. Decisions come
   from the nova_fsm it is given, exactly as for the bar. */
typedef struct nova_panel nova_panel_t;

#define NOVA_CARD_ROWS 4
typedef struct {
    const char *kind, *title;
    const char *lines[2];
    int n_lines;
    struct { const char *title, *sub; } rows[NOVA_CARD_ROWS];
    int n_rows, more;   /* more: items beyond the rows shown */
} nova_card_view_t;

nova_panel_t *nova_panel_create(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data);
/* Layout B's Dock Charm: the same panel as a glass bar anchored to the bottom of parent, the avatar standing partly
   above it, the answer growing upward (four reply lines), no notification dot. A card opens it and holds it open;
   without one it tucks NOVA_PANEL_TUCK_MS after the answer ends, or on the tuck button. The glass takes the
   speaker's tint. */
nova_panel_t *nova_panel_create_dock(lv_obj_t *parent, nova_fsm_t *fsm, nova_bar_fx_cb_t on_fx, void *user_data);
#define NOVA_PANEL_TUCK_MS 8000u
bool nova_panel_expanded(const nova_panel_t *p);              /* open, or opening */
void nova_panel_tuck(nova_panel_t *p);                        /* dock mode: close now, card or not */
typedef enum { NOVA_BTN_MIC, NOVA_BTN_END, NOVA_BTN_TUCK } nova_btn_t;
lv_obj_t *nova_panel_button(nova_panel_t *p, nova_btn_t b);
lv_obj_t *nova_panel_card(nova_panel_t *p);                   /* the answer card's box */
/* Dock: blur the glass behind the bar by radius px. GPU builds blur it by default (radius 22); --dock-blur N
   changes it and 0 turns it off. Set before the panel is built. The software renderer has no backdrop blur. */
void nova_panel_set_dock_blur(int radius);
lv_obj_t *nova_panel_reply(nova_panel_t *p);
lv_obj_t *nova_panel_bubble(nova_panel_t *p);                 /* dock: the "you" bubble; NULL elsewhere */
void nova_panel_apply(nova_panel_t *p, unsigned fx);
void nova_panel_set_text(nova_panel_t *p, const char *text, bool user);
void nova_panel_stream_text(nova_panel_t *p, const char *text);
void nova_panel_set_card(nova_panel_t *p, const nova_card_view_t *card);
void nova_panel_set_pending(nova_panel_t *p, bool pending);
/* Dock: the route's current instruction (NULL or "" when no route runs). While no turn is on it is the one-line
   text under "NOVA \xC2\xB7 NAVIGATING"; a turn takes the line over and the instruction returns when it ends or tucks. */
void nova_panel_set_route_line(nova_panel_t *p, const char *line);
/* Dock: a spoken route prompt. It replaces the instruction line, and never opens, tints, or times out the bar. */
void nova_panel_guidance(nova_panel_t *p, const char *text);
void nova_panel_greet(nova_panel_t *p);                       /* "Hi": the avatar hops and says hello */
void nova_panel_set_reduce_motion(nova_panel_t *p, bool on);
void nova_panel_set_max_height(nova_panel_t *p, int32_t h);    /* a card keeps the open panel within h */
lv_obj_t *nova_panel_obj(nova_panel_t *p);
lv_obj_t *nova_panel_avatar(nova_panel_t *p);
lv_obj_t *nova_panel_line(nova_panel_t *p);                  /* the state hint or the user's words */
bool nova_panel_is_animating(const nova_panel_t *p);
bool nova_panel_notified(const nova_panel_t *p);              /* the green dot is showing */
