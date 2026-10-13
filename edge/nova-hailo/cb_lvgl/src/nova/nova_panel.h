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
void nova_panel_apply(nova_panel_t *p, unsigned fx);
void nova_panel_set_text(nova_panel_t *p, const char *text, bool user);
void nova_panel_stream_text(nova_panel_t *p, const char *text);
void nova_panel_set_card(nova_panel_t *p, const nova_card_view_t *card);
void nova_panel_set_pending(nova_panel_t *p, bool pending);
void nova_panel_greet(nova_panel_t *p);                       /* "Hi": the avatar hops and says hello */
void nova_panel_set_reduce_motion(nova_panel_t *p, bool on);
void nova_panel_set_max_height(nova_panel_t *p, int32_t h);    /* a card keeps the open panel within h */
lv_obj_t *nova_panel_obj(nova_panel_t *p);
lv_obj_t *nova_panel_avatar(nova_panel_t *p);
lv_obj_t *nova_panel_line(nova_panel_t *p);                  /* the state hint or the user's words */
bool nova_panel_is_animating(const nova_panel_t *p);
bool nova_panel_notified(const nova_panel_t *p);              /* the green dot is showing */
