#pragma once
#include "lvgl.h"

/* One shared clock for decorative loops (breathing dots, fan pulse, EQ bars, tile glow, wiper sweep, the idle
   orb). Every subscriber steps in the same timer tick, so together they cost one redraw per tick instead of
   one per animation frame each; with the GPU renderer every redraw is a full frame. Subscribers compute their
   state from now_ms and should only touch objects whose value actually changed. Under reduce-motion nothing
   subscribes and decorations keep their resting look. */
#define CB_AMBIENT_MS 66

typedef void (*cb_ambient_cb_t)(void *ctx, uint32_t now_ms);

void cb_ambient_add(cb_ambient_cb_t cb, void *ctx);
void cb_ambient_remove(cb_ambient_cb_t cb, void *ctx);

/* 60 fps for motion the driver is watching (an active orb, the bar's tuck/expand): while at least one boost is
   held the display refresh and animation timers run every 16 ms, otherwise at LV_DEF_REFR_PERIOD. Calls nest;
   every boost(true) needs one boost(false). */
void cb_frame_boost(bool on);

/* 0..1000 raised cosine over `period_ms`, 0 at phase 0: a smooth breathing curve for subscribers. */
int32_t cb_ambient_wave(uint32_t now_ms, uint32_t period_ms, uint32_t phase_ms);
