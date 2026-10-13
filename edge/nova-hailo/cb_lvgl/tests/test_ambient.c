#include "cb_test.h"
#include "cb_ambient.h"

/* The motion budget: the shared ambient clock and the short frame-rate boost for the bar's tuck/expand. */

static uint32_t s_ms;
static int s_calls;

static uint32_t tick_cb(void) { return s_ms; }

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    LV_UNUSED(a);
    LV_UNUSED(px);
    lv_display_flush_ready(d);
}

static void count_step(void *ctx, uint32_t now)
{
    LV_UNUSED(ctx);
    LV_UNUSED(now);
    s_calls++;
}

static void run_ms(uint32_t ms)
{
    for(uint32_t t = 0; t < ms; t += 2) {
        s_ms += 2;
        lv_timer_handler();
    }
}

static void wave_is_a_raised_cosine(void)
{
    CHECK_EQ(cb_ambient_wave(0, 1000, 0), 0);
    CHECK_EQ(cb_ambient_wave(500, 1000, 0), 1000);
    CHECK_EQ(cb_ambient_wave(1000, 1000, 0), 0);
    CHECK_EQ(cb_ambient_wave(0, 1000, 500), 1000);   /* phase shifts the curve */
    int32_t q = cb_ambient_wave(250, 1000, 0);
    CHECK(q > 480 && q < 520);
}

static void subscribers_step_on_the_clock_until_removed(void)
{
    s_calls = 0;
    cb_ambient_add(count_step, NULL);
    run_ms(10 * CB_AMBIENT_MS);
    CHECK(s_calls >= 9 && s_calls <= 11);
    cb_ambient_remove(count_step, NULL);
    int before = s_calls;
    run_ms(5 * CB_AMBIENT_MS);
    CHECK_EQ(s_calls, before);
}

static int s_refr;

static void refr_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    s_refr++;
}

/* Refreshes in 330 ms with something invalidated every 2 ms: about 330 / period. */
static int refreshes(lv_obj_t *busy)
{
    s_refr = 0;
    for(int t = 0; t < 330; t += 2) {
        lv_obj_invalidate(busy);
        s_ms += 2;
        lv_timer_handler();
    }
    return s_refr;
}

static void boost_runs_frames_at_16_ms_while_held(lv_display_t *disp)
{
    lv_display_add_event_cb(disp, refr_cb, LV_EVENT_REFR_START, NULL);
    lv_obj_t *busy = lv_obj_create(lv_display_get_screen_active(disp));
    int normal = 330 / LV_DEF_REFR_PERIOD, fast = 330 / 16;

    int n = refreshes(busy);
    CHECK(n >= normal - 1 && n <= normal + 1);
    cb_frame_boost(true);
    cb_frame_boost(true);
    n = refreshes(busy);
    CHECK(n >= fast - 1 && n <= fast + 1);
    cb_frame_boost(false);
    n = refreshes(busy);
    CHECK(n >= fast - 1 && n <= fast + 1);   /* nested: one holder left */
    cb_frame_boost(false);
    cb_frame_boost(false);                   /* an extra release never drops below the default */
    n = refreshes(busy);
    CHECK(n >= normal - 1 && n <= normal + 1);
}

int main(void)
{
    lv_init();
    lv_tick_set_cb(tick_cb);
    static uint8_t buf[64 * 64 * 4];
    lv_display_t *disp = lv_display_create(64, 64);
    lv_display_set_flush_cb(disp, flush_cb);
    lv_display_set_buffers(disp, buf, NULL, sizeof buf, LV_DISPLAY_RENDER_MODE_PARTIAL);

    RUN(wave_is_a_raised_cosine);
    RUN(subscribers_step_on_the_clock_until_removed);
    boost_runs_frames_at_16_ms_while_held(disp);
    DONE();
}
