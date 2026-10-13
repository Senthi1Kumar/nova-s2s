#include "cb_ambient.h"
#include "cb_theme.h"

#define MAX_SUBS 32

static struct { cb_ambient_cb_t cb; void *ctx; } s_subs[MAX_SUBS];
static int s_n;
static lv_timer_t *s_timer;

static void tick_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    uint32_t now = lv_tick_get();
    for(int i = 0; i < s_n; i++) s_subs[i].cb(s_subs[i].ctx, now);
}

void cb_ambient_add(cb_ambient_cb_t cb, void *ctx)
{
    if(cb_reduce_motion() || !cb) return;
    if(s_n >= MAX_SUBS) {
        LV_LOG_WARN("ambient: more than %d subscribers", MAX_SUBS);
        return;
    }
    s_subs[s_n].cb = cb;
    s_subs[s_n].ctx = ctx;
    s_n++;
    if(!s_timer) s_timer = lv_timer_create(tick_cb, CB_AMBIENT_MS, NULL);
}

void cb_ambient_remove(cb_ambient_cb_t cb, void *ctx)
{
    for(int i = 0; i < s_n; i++) {
        if(s_subs[i].cb != cb || s_subs[i].ctx != ctx) continue;
        s_subs[i] = s_subs[--s_n];
        return;
    }
}

void cb_frame_boost(bool on)
{
    static int held;
    bool was = held > 0;
    held += on ? 1 : -1;
    if(held < 0) held = 0;
    if((held > 0) == was) return;
    uint32_t ms = held > 0 ? 16 : LV_DEF_REFR_PERIOD;
    lv_display_t *d = lv_display_get_default();
    if(d && lv_display_get_refr_timer(d)) lv_timer_set_period(lv_display_get_refr_timer(d), ms);
    if(lv_anim_get_timer()) lv_timer_set_period(lv_anim_get_timer(), ms);
}

int32_t cb_ambient_wave(uint32_t now_ms, uint32_t period_ms, uint32_t phase_ms)
{
    uint32_t p = (now_ms + phase_ms) % period_ms;
    int16_t angle = (int16_t)(p * 360u / period_ms);
    return 500 - (int32_t)lv_trigo_cos(angle) * 500 / LV_TRIGO_SIN_MAX;
}
