#pragma once
#include "live/nav_state.h"
#include "lvgl.h"
#include <stdbool.h>

/* Layout B "car + pages", filling the whole display: status strip; the car pane pinned on the left (speed,
   climate, device tiles); a swipe region on the right with Map / Media / Nova pages and a floating pager; the dock
   with driver temp -/+, the Nova bar slot, a mini player and passenger temp -/+. Metrics follow the 1080 p
   design and tighten below 900 px of height. */
typedef struct {
    void (*temp_step)(bool driver, int32_t delta_dc);   /* dock -/+ (sim: local; live: POST /v1/vehicle/set) */
} cockpit_hooks_t;

typedef struct {
    lv_obj_t *dock_mid;    /* Nova bar slot, centred in the dock */
    lv_obj_t *map;         /* map_view on the Map page */
    lv_obj_t *cards;       /* answer card stack on the Nova page (newest first) */
    lv_obj_t *nav_card;    /* map overlay: the next maneuver and the trip (hidden until navigation is active) */
    lv_obj_t *src_chip;    /* map overlay: position source (GPS / SIM / none) */
    lv_obj_t *link_chip;   /* status strip: pipeline link (Live / Replay / Simulated / Offline) */
    lv_obj_t *place;       /* status strip: where the car is (reverse-geocoded place, else coordinates) */
    lv_obj_t *toast;       /* transient answer card over the map */
    lv_obj_t *nova_slot;   /* v2: the Nova panel's place, top of the left column */
    int32_t nova_max_h;    /* v2: room for the open panel above the tiles */
} cockpit_t;

cockpit_t *cockpit_create(lv_obj_t *panel, const cockpit_hooks_t *hooks);
/* Layout v0.0.2: the same strip, pages and dock; the left column holds the Nova panel (nova_slot) over the device
   tiles, which glow with a moving gradient when they change; the dock's middle shows the speed. */
cockpit_t *cockpit_create_v2(lv_obj_t *panel, const cockpit_hooks_t *hooks);
cockpit_t *cockpit(void);
void cockpit_show_page(int idx);   /* 0 Map, 1 Media, 2 Nova */
void cockpit_set_chip(lv_obj_t *chip, const char *text, lv_color_t dot);
/* The turn-by-turn card over the map: maneuver arrow, distance to it, instruction, destination, "via" stops, the
   trip line and how much of the route is behind. NULL hides it. */
void cockpit_set_nav(const nav_banner_t *b, const char *via, int progress_permille);
