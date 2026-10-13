#include "cb_model.h"
#include <stdio.h>
#include <string.h>

static cb_model_t s_m;
static bool s_inited;

typedef struct { const char *title; int32_t duration_s; } track_t;
static const track_t k_tracks[] = {
    { "Ilaiyaraaja \xE2\x80\x94 Raja Raja Chozhan", 271 },
    { "A. R. Rahman \xE2\x80\x94 Nenjukkule", 256 },
    { "Debussy \xE2\x80\x94 Clair de Lune", 301 },
};
#define TRACK_COUNT ((int32_t)(sizeof(k_tracks) / sizeof(k_tracks[0])))
static char s_media_title[96];

static const cb_last_turn_t k_initial_turn = {
    .utterance = "make it a bit warmer for me and call Sam",
    .router = "jev",
    .latency = "118 + 131 ms",
    .calls = { { "hvac \xC2\xB7 set_temperature", "driver +1 \xC2\xB0" "C", CB_VERDICT_EXECUTE },
               { "phone \xC2\xB7 call_contact", "Sam", CB_VERDICT_EXECUTE } },
    .n_calls = 2,
    .diffs = { { "HVAC\xE2\x80\xA6" "Driver.Temperature", "20.0 \xE2\x86\x92 21.0" },
               { "Nova.Phone.CallState", "idle \xE2\x86\x92 dialing" } },
    .n_diffs = 2,
};

static int32_t clamp(int32_t v, int32_t lo, int32_t hi) { return v < lo ? lo : (v > hi ? hi : v); }

static lv_subject_t *mk_int(int32_t v)
{
    lv_subject_t *s = lv_subject_create(LV_SUBJECT_TYPE_INT);
    lv_subject_set_int(s, v);
    return s;
}

cb_model_t *cb_model(void) { return &s_m; }

void cb_model_init(void)
{
    if(s_inited) return;
    s_inited = true;
    s_m.speed_kph = mk_int(62);
    s_m.gear = mk_int(CB_GEAR_D);
    s_m.soc_pct = mk_int(68);
    s_m.range_km = mk_int(68 * 309 / 100);
    s_m.route_permille = mk_int(150);
    s_m.eta_min = mk_int(35);
    s_m.driver_temp_dc = mk_int(200);
    s_m.passenger_temp_dc = mk_int(200);
    s_m.fan_level = mk_int(2);
    s_m.ac_on = mk_int(1);
    s_m.win_pct[CB_WIN_FL] = mk_int(0);
    s_m.win_pct[CB_WIN_FR] = mk_int(0);
    s_m.win_pct[CB_WIN_RL] = mk_int(10);
    s_m.win_pct[CB_WIN_RR] = mk_int(10);
    s_m.sunroof_pct = mk_int(15);
    s_m.wipers_on = mk_int(0);
    s_m.wiper_mode = mk_int(0);
    s_m.headlights = mk_int(1);
    s_m.defrost = mk_int(0);
    s_m.mirrors_folded = mk_int(0);
    s_m.drive_mode = mk_int(1);
    s_m.ambient_color = mk_int(0);
    s_m.ambient_level = mk_int(50);
    s_m.locked = mk_int(1);
    s_m.seat_heat = mk_int(1);
    s_m.media_playing = mk_int(1);
    s_m.media_track = mk_int(0);
    s_m.media_pos_s = mk_int(102);
    s_m.volume = mk_int(40);
    s_m.media_title_rev = mk_int(0);
    s_m.call_active = mk_int(1);
    s_m.call_s = mk_int(12);
    s_m.session_s = mk_int(292);
    s_m.last_turn = lv_subject_create(LV_SUBJECT_TYPE_POINTER);
    lv_subject_set_pointer(s_m.last_turn, (void *)&k_initial_turn);
}

void cb_model_set_int(lv_subject_t *s, int32_t v)
{
    if(lv_subject_get_int(s) != v) lv_subject_set_int(s, v);
}

void cb_model_set_speed(int32_t kph) { cb_model_set_int(s_m.speed_kph, clamp(kph, 0, 250)); }

void cb_model_set_soc(int32_t pct)
{
    pct = clamp(pct, 0, 100);
    cb_model_set_int(s_m.soc_pct, pct);
    cb_model_set_int(s_m.range_km, pct * 309 / 100);
}

void cb_model_set_temp(bool driver, int32_t deci_c)
{
    cb_model_set_int(driver ? s_m.driver_temp_dc : s_m.passenger_temp_dc, clamp(deci_c, 160, 300));
}

void cb_model_set_window(cb_window_t w, int32_t pct)
{
    if(w >= CB_WIN_COUNT) return;
    cb_model_set_int(s_m.win_pct[w], clamp(pct, 0, 100));
}

void cb_model_set_sunroof(int32_t pct) { cb_model_set_int(s_m.sunroof_pct, clamp(pct, 0, 100)); }

void cb_model_set_last_turn(const cb_last_turn_t *t)
{
    if(t && lv_subject_get_pointer(s_m.last_turn) != t) lv_subject_set_pointer(s_m.last_turn, (void *)t);
}

static int32_t track_index(int32_t idx) { return ((idx % TRACK_COUNT) + TRACK_COUNT) % TRACK_COUNT; }

const char *cb_model_track_title(int32_t idx) { return k_tracks[track_index(idx)].title; }

int32_t cb_model_track_duration(int32_t idx) { return k_tracks[track_index(idx)].duration_s; }

void cb_model_set_media_title(const char *title)
{
    if(!title) title = "";
    if(!strcmp(s_media_title, title)) return;
    snprintf(s_media_title, sizeof(s_media_title), "%s", title);
    cb_model_set_int(s_m.media_title_rev, lv_subject_get_int(s_m.media_title_rev) + 1);
}

const char *cb_model_media_title(void) { return s_media_title; }
