#include "cb_test.h"
#include "cb_model.h"
#include "cb_sim.h"
#include <string.h>

#define GET(field) lv_subject_get_int(cb_model()->field)

static int s_count;
static void count_cb(lv_observer_t *o, lv_subject_t *s) { LV_UNUSED(o); LV_UNUSED(s); s_count++; }

static void initial_values(void) {
    CHECK_EQ(GET(speed_kph), 62); CHECK_EQ(GET(gear), CB_GEAR_D);
    CHECK_EQ(GET(soc_pct), 68); CHECK_EQ(GET(range_km), 210);
    CHECK_EQ(GET(win_pct[CB_WIN_RL]), 10); CHECK_EQ(GET(sunroof_pct), 15);
    CHECK_EQ(GET(driver_temp_dc), 200); CHECK_EQ(GET(media_playing), 1);
    const cb_last_turn_t *t = lv_subject_get_pointer(cb_model()->last_turn);
    CHECK(t != NULL);
    CHECK(t && strcmp(t->utterance, "make it a bit warmer for me and call Sam") == 0);
}
static void clamps(void) {
    cb_model_set_speed(300); CHECK_EQ(GET(speed_kph), 250);
    cb_model_set_speed(-5);  CHECK_EQ(GET(speed_kph), 0);
    cb_model_set_soc(150);   CHECK_EQ(GET(soc_pct), 100); CHECK_EQ(GET(range_km), 309);
    cb_model_set_soc(68);    CHECK_EQ(GET(soc_pct), 68);  CHECK_EQ(GET(range_km), 210);
    cb_model_set_window(CB_WIN_FR, 140); CHECK_EQ(GET(win_pct[CB_WIN_FR]), 100);
    cb_model_set_window(CB_WIN_FR, -1);  CHECK_EQ(GET(win_pct[CB_WIN_FR]), 0);
    cb_model_set_temp(true, 400);  CHECK_EQ(GET(driver_temp_dc), 300);
    cb_model_set_temp(false, 100); CHECK_EQ(GET(passenger_temp_dc), 160);
    cb_model_set_sunroof(101); CHECK_EQ(GET(sunroof_pct), 100);
    cb_model_set_speed(62);
}
static void notifies_once_per_change(void) {
    lv_subject_add_observer(cb_model()->win_pct[CB_WIN_RL], count_cb, NULL);   /* LVGL notifies once on add */
    int base = s_count;
    cb_model_set_window(CB_WIN_RL, 40); CHECK_EQ(s_count, base + 1);
    cb_model_set_window(CB_WIN_RL, 40); CHECK_EQ(s_count, base + 1);
    cb_model_set_window(CB_WIN_RL, 10); CHECK_EQ(s_count, base + 2);
}
static void sim_keeps_speed_in_band(void) {
    cb_sim_start(1);
    for(int i = 0; i < 200; i++) {
        cb_sim_step();
        int32_t v = GET(speed_kph);
        CHECK(v >= 48 && v <= 78);
        if(v < 48 || v > 78) break;
    }
}
static void sim_advances_media(void) {
    cb_model_set_int(cb_model()->media_track, 0);
    cb_model_set_int(cb_model()->media_pos_s, 0);
    cb_model_set_int(cb_model()->media_playing, 1);
    for(int i = 0; i < 10; i++) cb_sim_step();
    CHECK_EQ(GET(media_pos_s), 10);
    cb_model_set_int(cb_model()->media_pos_s, cb_model_track_duration(0) - 1);
    cb_sim_step();
    CHECK_EQ(GET(media_track), 1); CHECK_EQ(GET(media_pos_s), 0);
}
static void track_wraps(void) {
    CHECK(strcmp(cb_model_track_title(3), cb_model_track_title(0)) == 0);
    CHECK_EQ(cb_model_track_duration(4), cb_model_track_duration(1));
}
int main(void) {
    lv_init();
    cb_model_init();
    cb_model_init();   /* idempotent */
    RUN(initial_values); RUN(clamps); RUN(notifies_once_per_change);
    RUN(sim_keeps_speed_in_band); RUN(sim_advances_media); RUN(track_wraps);
    DONE();
}
