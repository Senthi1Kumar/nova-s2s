#pragma once
#include "lvgl.h"
#include "net.h"
#include "theme.h"
#include "ui_kit.h"

/* A page of the Studio. build() fills `parent` once; tick() runs for the visible page at 2 Hz with a fresh copy
   of the shared model; show() fires when the page becomes visible (on = true) or hidden. */
typedef struct {
    const char *nav;       /* sidebar label */
    ui_icon_t icon;        /* sidebar icon */
    const char *eyebrow;   /* small caps line above the title */
    const char *title;
    const char *subtitle;
    void (*build)(lv_obj_t *parent);
    void (*tick)(const net_snapshot_t *s);
    void (*show)(bool on);
} app_page_t;

extern const app_page_t page_voice, page_overview, page_vision, page_chat, page_face, page_device, page_inspector;

typedef struct {
    int page;
    bool reduce_motion;
} app_cfg_t;

void app_build(lv_obj_t *root, const app_cfg_t *cfg);
void app_goto(int idx);
int32_t app_content_w(void);   /* usable width of a page, valid during build() */
int app_page(void);
int app_page_count(void);
void vision_demo(const char *task, const char *source);   /* preselect the Vision page's task and source (either may be NULL) */
void vision_cycle(uint32_t period_ms);   /* test hook: change task then source every period through the dropdown event path */
void chat_demo_focus(void);   /* focus the chat field 3 s after the page is shown, as a tap would (shows the keyboard) */
void chat_demo(const char *text);   /* send this question once the Chat page is shown (screenshots, smoke tests) */
void app_request_quit(void);
bool app_quit_requested(void);
lv_obj_t *app_key_sink(void);   /* focus target when no text input is active */

/* shared page helpers */
lv_obj_t *app_tile(lv_obj_t *parent, const char *label, lv_obj_t **value, lv_obj_t **bar, int32_t w_pct);
lv_obj_t *app_card(lv_obj_t *parent, const char *title);   /* card with an uppercase caption; returns the card */
lv_obj_t *app_flow(lv_obj_t *parent, int32_t gap);         /* row that wraps */
void app_table_fit(lv_obj_t *table, lv_obj_t *container, const int *pct, int n);   /* column widths in percent of the container */
void app_fmt_ms(char *out, size_t cap, double ms);
