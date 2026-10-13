#include "cb_sim.h"
#include "cb_model.h"

static uint32_t s_rng = 1;
static uint32_t s_steps;
static lv_timer_t *s_timer;

static uint32_t rnd(void)
{
    s_rng = s_rng * 1103515245u + 12345u;
    return (s_rng >> 16) & 0x7fffu;
}

#define GET(field) lv_subject_get_int(m->field)

void cb_sim_step(void)
{
    cb_model_t *m = cb_model();

    int32_t speed = GET(speed_kph) + (int32_t)(rnd() % 5u) - 2;
    cb_model_set_speed(LV_CLAMP(48, speed, 78));

    if(++s_steps % 300u == 0) cb_model_set_soc(GET(soc_pct) - 1);

    if(GET(media_playing)) {
        int32_t pos = GET(media_pos_s) + 1;
        if(pos >= cb_model_track_duration(GET(media_track))) {
            cb_model_set_int(m->media_track, (GET(media_track) + 1) % 3);
            pos = 0;
        }
        cb_model_set_int(m->media_pos_s, pos);
    }

    if(GET(call_active)) cb_model_set_int(m->call_s, GET(call_s) + 1);

    int32_t route = (GET(route_permille) + 3) % 1000;
    cb_model_set_int(m->route_permille, route);
    cb_model_set_int(m->eta_min, 41 * (1000 - route) / 1000);

    cb_model_set_int(m->session_s, GET(session_s) + 1);
}

static void sim_timer_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    cb_sim_step();
}

void cb_sim_start(uint32_t seed)
{
    s_rng = seed ? seed : 1u;
    if(!s_timer) s_timer = lv_timer_create(sim_timer_cb, 1000, NULL);
}
