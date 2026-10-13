#include "nova/avatar.h"
#include "cb_ambient.h"
#include "cb_theme.h"
#include "nova/avatar_model.h"
#include "src/misc/cache/lv_cache.h"   /* lv_image_cache_drop(): not exported by v9.6's public headers */
#include <math.h>

#define MARGIN 0.2f   /* room around the face for the halo, the antenna and the hops, in face sizes */
#define FAST_MS 33

typedef struct { float r, g, b; } rgb_t;
typedef struct { float r, g, b, a; } px_t;   /* premultiplied while layers stack up */

typedef struct {
    lv_obj_t *img;
    lv_draw_buf_t *buf;
    int32_t face, size;
    av_model_t m;
    lv_timer_t *fast;
    bool boosted;
} avatar_t;

#define HEX(h) ((rgb_t){ (float)(((h) >> 16) & 0xff) / 255.0f, (float)(((h) >> 8) & 0xff) / 255.0f, \
                         (float)((h) & 0xff) / 255.0f })

static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
static float lerpf(float a, float b, float t) { return a + (b - a) * t; }
static float cov(float d) { return clampf(0.5f - d, 0.0f, 1.0f); }   /* pixel coverage of a signed distance */
static rgb_t mix(rgb_t a, rgb_t b, float t) { return (rgb_t){ lerpf(a.r, b.r, t), lerpf(a.g, b.g, t), lerpf(a.b, b.b, t) }; }
static uint8_t to_byte(float v) { return (uint8_t)(v <= 0.0f ? 0 : v >= 1.0f ? 255 : (int)(v * 255.0f + 0.5f)); }

static void over(px_t *p, rgb_t c, float a)
{
    if(a <= 0.0f) return;
    float k = 1.0f - a;
    p->r = c.r * a + p->r * k;
    p->g = c.g * a + p->g * k;
    p->b = c.b * a + p->b * k;
    p->a = a + p->a * k;
}

/* Signed distances in pixels (negative inside). */
static float sd_rbox(float x, float y, float bx, float by, float r)
{
    float qx = fabsf(x) - bx + r, qy = fabsf(y) - by + r;
    float ox = qx > 0.0f ? qx : 0.0f, oy = qy > 0.0f ? qy : 0.0f;
    return sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0.0f) - r;
}

static float sd_ellipse(float x, float y, float rx, float ry)
{
    return (sqrtf(x * x / (rx * rx) + y * y / (ry * ry)) - 1.0f) * fminf(rx, ry);
}

/* The upper (up) or lower half of a ring around (cx, cy), radius rr, half-thickness th, with round ends. */
static float sd_half_ring(float x, float y, float cx, float cy, float rr, float th, bool up)
{
    float dx = x - cx, dy = y - cy;
    if(up ? dy <= 0.0f : dy >= 0.0f) return fabsf(sqrtf(dx * dx + dy * dy) - rr) - th;
    float ex = fabsf(dx) - rr;
    return sqrtf(ex * ex + dy * dy) - th;
}

static float sd_hline(float x, float y, float cx, float cy, float half, float th)
{
    float dx = fmaxf(fabsf(x - cx) - half, 0.0f), dy = y - cy;
    return sqrtf(dx * dx + dy * dy) - th;
}

static void render(avatar_t *v, uint32_t now)
{
    av_pose_t q = av_model_step(&v->m, now);
    const float D = (float)v->face, c = (float)(v->size - 1) * 0.5f;
    const float ct = cosf(q.tilt), st = sinf(q.tilt);
    const rgb_t glow = { q.glow_rgb[0], q.glow_rgb[1], q.glow_rgb[2] };
    const rgb_t eye = mix(HEX(0x7ff3ff), HEX(0xff5f6d), q.red), white = { 1.0f, 1.0f, 1.0f };
    const rgb_t shell = HEX(0xf6f9fc), shade = HEX(0xb9c5d5), pod = HEX(0xd3dbe6);
    const rgb_t visor_top = HEX(0x1d2536), visor_bot = HEX(0x05080d);

    /* proportions of the face size: a wide round head, the visor across it, a stalk and light on top */
    const float hx = 0.5f * D, hy = 0.43f * D, hr = 0.34f * D;
    const float vy = 0.03f * D, vx = 0.38f * D, vh = 0.25f * D, vr = 0.2f * D;
    const float re = 0.072f * D, eye_dx = 0.15f * D, eye_shift = q.look_x * 0.05f * D;
    const float ey = vy - 0.02f * D + q.look_y * 0.04f * D, eye_ry = fmaxf(re * q.eye_open, 0.014f * D);
    const float my = 0.17f * D, glow_w = 0.12f * D;
    const float ant_top = -0.55f * D, ant_r = 0.035f * D;

    uint32_t stride = v->buf->header.stride;
    for(int32_t y = 0; y < v->size; y++) {
        uint8_t *out = v->buf->data + (size_t)y * stride;
        for(int32_t x = 0; x < v->size; x++, out += 4) {
            float sx = (float)x - c, sy = (float)y - c - q.bob * D;
            float px = ct * sx + st * sy, py = -st * sx + ct * sy;   /* into the head's frame (undo the tilt) */
            float d_head = sd_rbox(px, py, hx, hy, hr);
            float d_ball = sqrtf(px * px + (py - ant_top) * (py - ant_top)) - ant_r;
            if(d_head > glow_w && d_ball > 0.6f * glow_w) {
                out[0] = out[1] = out[2] = out[3] = 0;
                continue;
            }
            px_t o = { 0 };

            if(d_head > 0.0f) {   /* halo */
                float t = clampf(d_head / glow_w, 0.0f, 1.0f);
                over(&o, glow, q.glow * 0.55f * (1.0f - t) * (1.0f - t));
            }
            if(d_ball > 0.0f) over(&o, glow, q.glow * 0.7f * expf(-d_ball / (0.025f * D)));
            float d_stalk = fmaxf(fabsf(px) - 0.012f * D, fmaxf(ant_top - py, py + hy - 0.02f * D));
            over(&o, shade, cov(d_stalk));
            over(&o, mix(glow, white, 0.35f), cov(d_ball));

            float d_pod = sd_rbox(fabsf(px) - 0.5f * D, py, 0.055f * D, 0.15f * D, 0.05f * D);
            over(&o, mix(pod, glow, 0.25f * clampf(-py / (0.15f * D) + 0.5f, 0.0f, 1.0f)), cov(d_pod));

            float a_head = cov(d_head);
            if(a_head > 0.0f) {
                float s = clampf(0.45f + 0.30f * px / hx + 0.45f * py / hy, 0.0f, 1.0f);
                rgb_t col = mix(shell, shade, s * s);
                float gx = (px + 0.22f * D) / (0.14f * D), gy = (py + 0.25f * D) / (0.07f * D);
                col = mix(col, white, 0.85f * expf(-(gx * gx + gy * gy)));   /* gloss */
                over(&o, col, a_head);
            }

            float a_vis = cov(sd_rbox(px, py - vy, vx, vh, vr));
            if(a_vis > 0.0f) {
                rgb_t col = mix(visor_top, visor_bot, clampf((py - vy + vh) / (2.0f * vh), 0.0f, 1.0f));
                float band = (0.35f * px + py - vy + 0.14f * D) / (0.05f * D);
                col = mix(col, HEX(0x9aaecc), 0.10f * expf(-band * band));   /* glass sheen */
                float ox = fabsf(px - eye_shift) - eye_dx, oy = py - ey;
                float de = sqrtf(ox * ox + oy * oy) - re;
                if(de > 0.0f) col = mix(col, eye, 0.45f * expf(-de / (0.045f * D)));   /* the eyes light the glass */
                over(&o, col, a_vis);

                float a_round = cov(sd_ellipse(ox, oy, 0.9f * re, eye_ry));
                float a_arc = cov(sd_half_ring(ox, oy, 0.0f, 0.03f * D, 0.058f * D, 0.017f * D, true));
                over(&o, eye, lerpf(a_round, a_arc, q.eye_happy) * a_vis);

                float a_m = lerpf(cov(sd_hline(px, py, 0.0f, my, 0.05f * D, 0.012f * D)),
                                  cov(sd_half_ring(px, py, 0.0f, my - 0.045f * D, 0.06f * D, 0.012f * D, false)),
                                  q.smile);
                float o3 = clampf(q.mouth_open * 3.0f, 0.0f, 1.0f);
                if(o3 > 0.0f) {
                    float ry = 0.012f * D + 0.045f * D * q.mouth_open;
                    a_m = lerpf(a_m, cov(sd_ellipse(px, py - my - 0.005f * D, 0.055f * D, ry)), o3);
                }
                over(&o, mix(eye, white, 0.1f), 0.9f * a_m * a_vis);
            }

            if(o.a <= 0.0f) {
                out[0] = out[1] = out[2] = out[3] = 0;
                continue;
            }
            out[0] = to_byte(o.b / o.a);
            out[1] = to_byte(o.g / o.a);
            out[2] = to_byte(o.r / o.a);
            out[3] = to_byte(o.a);
        }
    }
    lv_image_cache_drop(v->buf);   /* GPU draw units cache textures by buffer, not by content */
    lv_obj_invalidate(v->img);
}

static bool needs_fast(const avatar_t *v, uint32_t now) { return !cb_reduce_motion() && av_model_animating(&v->m, now); }

static void set_fast(avatar_t *v, bool on)
{
    if(on == v->boosted) return;
    v->boosted = on;
    cb_frame_boost(on);
    if(on) lv_timer_resume(v->fast);
    else lv_timer_pause(v->fast);
}

static void fast_cb(lv_timer_t *t)
{
    avatar_t *v = lv_timer_get_user_data(t);
    uint32_t now = lv_tick_get();
    render(v, now);
    if(!needs_fast(v, now)) set_fast(v, false);
}

static void ambient_step(void *ctx, uint32_t now)
{
    avatar_t *v = ctx;
    if(v->boosted || cb_reduce_motion()) return;
    render(v, now);
    if(needs_fast(v, now)) set_fast(v, true);   /* a blink just started */
}

static void kick(avatar_t *v)
{
    uint32_t now = lv_tick_get();
    if(needs_fast(v, now)) set_fast(v, true);
    else render(v, now);
}

static void ext_draw_cb(lv_event_t *e)
{
    avatar_t *v = lv_event_get_user_data(e);
    lv_event_set_ext_draw_size(e, (v->size - v->face) / 2 + 1);
}

static void delete_cb(lv_event_t *e)
{
    avatar_t *v = lv_event_get_user_data(e);
    cb_ambient_remove(ambient_step, v);
    set_fast(v, false);
    lv_timer_delete(v->fast);
    lv_draw_buf_destroy(v->buf);
    lv_free(v);
}

lv_obj_t *avatar_create(lv_obj_t *parent, int32_t face)
{
    avatar_t *v = lv_malloc_zeroed(sizeof(*v));
    LV_ASSERT_MALLOC(v);
    v->face = face;
    v->size = face + 2 * (int32_t)(MARGIN * (float)face + 0.5f);
    av_model_init(&v->m, (uint32_t)(uintptr_t)v, lv_tick_get());
    v->buf = lv_draw_buf_create((uint32_t)v->size, (uint32_t)v->size, LV_COLOR_FORMAT_ARGB8888, 0);
    LV_ASSERT_MALLOC(v->buf);
    lv_draw_buf_clear(v->buf, NULL);

    lv_obj_t *cont = lv_obj_create(parent);
    lv_obj_remove_style_all(cont);
    lv_obj_set_size(cont, face, face);
    lv_obj_set_overflow_visible(cont, true);
    lv_obj_set_scrollable(cont, false);
    lv_obj_set_clickable(cont, true);
    lv_obj_set_user_data(cont, v);
    lv_obj_add_event_cb(cont, ext_draw_cb, LV_EVENT_REFR_EXT_DRAW_SIZE, v);
    lv_obj_refresh_ext_draw_size(cont);

    v->img = lv_image_create(cont);
    lv_obj_set_clickable(v->img, false);
    lv_image_set_src(v->img, v->buf);
    lv_obj_center(v->img);

    v->fast = lv_timer_create(fast_cb, FAST_MS, v);
    lv_timer_pause(v->fast);
    cb_ambient_add(ambient_step, v);
    lv_obj_add_event_cb(cont, delete_cb, LV_EVENT_DELETE, v);
    render(v, lv_tick_get());
    return cont;
}

void avatar_set_state(lv_obj_t *av, nv_activity_t a, bool muted)
{
    avatar_t *v = lv_obj_get_user_data(av);
    if(!v) return;
    uint32_t now = lv_tick_get();
    av_model_set_mood(&v->m, av_mood_for(a, muted), cb_reduce_motion() ? now - AV_BLEND_MS : now);
    kick(v);
}

void avatar_set_level(lv_obj_t *av, uint8_t level)
{
    avatar_t *v = lv_obj_get_user_data(av);
    if(v) av_model_set_level(&v->m, level);
}

void avatar_greet(lv_obj_t *av)
{
    avatar_t *v = lv_obj_get_user_data(av);
    if(!v || cb_reduce_motion()) return;
    av_model_greet(&v->m, lv_tick_get());
    kick(v);
}
