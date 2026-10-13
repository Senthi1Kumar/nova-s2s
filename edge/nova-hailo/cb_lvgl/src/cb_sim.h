#pragma once
#include <stdint.h>

/* 1 Hz drift of the simulated cabin: speed random walk in 48..78 km/h, SoC -1 % every 300 s, media
   position (next track at the end), call timer, route progress, session clock. Deterministic per seed. */
void cb_sim_start(uint32_t seed);   /* seeds the generator and starts a 1 s lv_timer (once) */
void cb_sim_step(void);             /* one tick; tests call it directly */
