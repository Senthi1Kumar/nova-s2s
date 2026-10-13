#include "nova/orb.h"
#include "cb_ambient.h"
#include "cb_theme.h"
#include "src/misc/cache/lv_cache.h"   /* lv_image_cache_drop(): not exported by v9.6's public headers */
#include <math.h>

#define GLOW_PAD 8     /* glow margin around the body; 8 keeps it inside the bar's rounded end */
#define ACTIVE_MS 16   /* 60 fps (with a frame boost) while listening, thinking or speaking */
#define BLEND_MS 450   /* palette cross-fade between states */
#define BLOOM_MS 700   /* wake bloom */
#define TAU 6.2831853f

typedef struct { float r, g, b; } rgb_t;

typedef struct {
    rgb_t base;      /* state colour the white highlight swirls into; also the glow colour */
    rgb_t spot[3];   /* soft colour spots drifting inside the sphere */
    float swirl_s;   /* seconds per revolution of the colour field */
    float glow;      /* resting glow strength 0..1 */
    float breath;    /* radius breathing amplitude, fraction of the radius */
    float breath_s;  /* breathing period, seconds */
} palette_t;

#define RGB(h) { (float)(((h) >> 16) & 0xff) / 255.0f, (float)(((h) >> 8) & 0xff) / 255.0f, (float)((h) & 0xff) / 255.0f }

enum { P_IDLE, P_LISTEN, P_THINK, P_SPEAK, P_MUTED, P_COUNT };

static const palette_t k_pal[P_COUNT] = {
    [P_IDLE] = { RGB(0x5d6678), { RGB(0x7b8496), RGB(0x4d5566), RGB(0x9aa2b3) }, 16.0f, 0.00f, 0.010f, 5.0f },
    [P_LISTEN] = { RGB(0x4cd6ff), { RGB(0x7fe3ff), RGB(0x3fb8ff), RGB(0x9ff0ff) }, 9.0f, 0.38f, 0.000f, 1.0f },
    [P_THINK] = { RGB(0xb66bff), { RGB(0xd08bff), RGB(0x9a5bff), RGB(0xe0b8ff) }, 4.0f, 0.42f, 0.012f, 1.2f },
    [P_SPEAK] = { RGB(0x4dffb4), { RGB(0x7dffc8), RGB(0x3fe6a8), RGB(0xa8ffd9) }, 7.0f, 0.45f, 0.025f, 1.6f },
    [P_MUTED] = { RGB(0x5d6678), { RGB(0x6b7385), RGB(0x4d5566), RGB(0x7b8496) }, 24.0f, 0.00f, 0.000f, 1.0f },
};

/* Spot orbits as fractions of the radius, relative angular speeds and starting angles. */
static const float k_orbit[3] = { 0.46f, 0.36f, 0.28f };
static const float k_speed[3] = { 1.0f, -0.7f, 0.45f };
static const float k_phase[3] = { 0.0f, 2.1f, 4.2f };
static const rgb_t k_white = RGB(0xf7fafc);

typedef struct {
    lv_obj_t *img;
    lv_draw_buf_t *buf;
    int32_t d, size;
    palette_t from, to;
    uint32_t blend_t0, bloom_t0, last;
    bool blending, blooming;
    nv_activity_t act;
    bool muted;
    uint8_t level;
    float swirl, breath;   /* integrated phases (revolutions), so a palette change never jumps */
    lv_timer_t *fast;
    bool boosted;   /* holding a cb_frame_boost while the fast timer runs */
} orb_t;

static float lerpf(float a, float b, float t) { return a + (b - a) * t; }

static rgb_t mix(rgb_t a, rgb_t b, float t) { return (rgb_t){ lerpf(a.r, b.r, t), lerpf(a.g, b.g, t), lerpf(a.b, b.b, t) }; }

static palette_t palette_mix(const palette_t *a, const palette_t *b, float t)
{
    palette_t p;
    p.base = mix(a->base, b->base, t);
    for(int i = 0; i < 3; i++) p.spot[i] = mix(a->spot[i], b->spot[i], t);
    p.swirl_s = lerpf(a->swirl_s, b->swirl_s, t);
    p.glow = lerpf(a->glow, b->glow, t);
    p.breath = lerpf(a->breath, b->breath, t);
    p.breath_s = lerpf(a->breath_s, b->breath_s, t);
    return p;
}

static int palette_for(nv_activity_t a, bool muted)
{
    if(muted) return P_MUTED;
    switch(a) {
        case NV_LISTENING: return P_LISTEN;
        case NV_THINKING: return P_THINK;
        case NV_SPEAKING: return P_SPEAK;
        default: return P_IDLE;
    }
}

static bool is_active(const orb_t *o) { return !o->muted && o->act != NV_IDLE; }

static uint8_t to_byte(float v) { return (uint8_t)(v <= 0.0f ? 0 : v >= 1.0f ? 255 : (int)(v * 255.0f + 0.5f)); }

static void render(orb_t *o, uint32_t now)
{
    float dt = cb_reduce_motion() ? 0.0f : (float)(now - o->last) / 1000.0f;
    if(dt > 0.2f) dt = 0.2f;
    o->last = now;

    float k = 1.0f;
    if(o->blending) {
        k = (float)(now - o->blend_t0) / BLEND_MS;
        if(k >= 1.0f || cb_reduce_motion()) {
            k = 1.0f;
            o->blending = false;
        }
        k = k * k * (3.0f - 2.0f * k);
    }
    palette_t p = palette_mix(&o->from, &o->to, k);

    o->swirl += dt / p.swirl_s;
    o->breath += dt / p.breath_s;
    float br = p.breath * sinf(TAU * o->breath);
    if(o->level && o->act == NV_SPEAKING) br = p.breath * (2.0f * (float)o->level / 255.0f - 1.0f);
    float r_body = (float)o->d * 0.5f * (1.0f + br);
    float glow = p.glow + 4.0f * br;
    if(o->blooming) {
        float q = (float)(now - o->bloom_t0) / BLOOM_MS;
        if(q >= 1.0f) o->blooming = false;
        else glow += 0.9f * (q < 0.15f ? q / 0.15f : (1.0f - q) * (1.0f - q) / 0.7225f);
    }
    if(glow > 1.0f) glow = 1.0f;

    float th = TAU * o->swirl;
    float ux = cosf(th), uy = sinf(th);
    float sx[3], sy[3];
    for(int i = 0; i < 3; i++) {
        float a = TAU * o->swirl * k_speed[i] + k_phase[i];
        sx[i] = k_orbit[i] * cosf(a);
        sy[i] = k_orbit[i] * sinf(a) * 0.9f;
    }

    float c = (float)(o->size - 1) * 0.5f;
    float r_out = r_body + GLOW_PAD;
    float inv_r = 1.0f / r_body;
    uint32_t stride = o->buf->header.stride;
    for(int32_t y = 0; y < o->size; y++) {
        uint8_t *px = o->buf->data + (size_t)y * stride;
        float dy = (float)y - c;
        for(int32_t x = 0; x < o->size; x++, px += 4) {
            float dx = (float)x - c;
            float rho2 = dx * dx + dy * dy;
            if(rho2 >= r_out * r_out) {
                px[0] = px[1] = px[2] = px[3] = 0;
                continue;
            }
            float rho = sqrtf(rho2);
            float a_body = r_body + 0.5f - rho;
            a_body = a_body < 0.0f ? 0.0f : a_body > 1.0f ? 1.0f : a_body;
            float a_glow = 0.0f;
            if(glow > 0.0f && rho > r_body - 1.0f) {
                float q = (rho - r_body) / GLOW_PAD;
                q = q < 0.0f ? 0.0f : q;
                a_glow = glow * (1.0f - q) * (1.0f - q);
            }
            float alpha = a_body + a_glow * (1.0f - a_body);
            if(alpha <= 0.0f) {
                px[0] = px[1] = px[2] = px[3] = 0;
                continue;
            }

            rgb_t col = p.base;
            if(a_body > 0.0f) {
                float nx = dx * inv_r, ny = dy * inv_r;
                /* The original white -> state colour gradient, its axis turning with the swirl so the white
                   travels around the orb, plus soft same-hue spots drifting through it. Flat colour, no shading:
                   lighting and a specular spot read as metal. */
                float g = 0.5f + 0.5f * (nx * ux + ny * uy);
                col = mix(k_white, p.base, g);
                for(int i = 0; i < 3; i++) {
                    float ex = nx - sx[i], ey = ny - sy[i];
                    float w = 1.0f / (1.0f + 6.0f * (ex * ex + ey * ey));
                    col = mix(col, p.spot[i], 0.5f * w * w * g);
                }
            }
            float wb = a_body / alpha, wg = 1.0f - wb;
            px[0] = to_byte(col.b * wb + p.base.b * wg);
            px[1] = to_byte(col.g * wb + p.base.g * wg);
            px[2] = to_byte(col.r * wb + p.base.r * wg);
            px[3] = to_byte(alpha);
        }
    }
    lv_image_cache_drop(o->buf);   /* GPU draw units cache textures by buffer, not by content */
    lv_obj_invalidate(o->img);
}

static bool needs_fast(const orb_t *o) { return !cb_reduce_motion() && (is_active(o) || o->blending || o->blooming); }

static void set_fast(orb_t *o, bool on)
{
    if(on == o->boosted) return;
    o->boosted = on;
    cb_frame_boost(on);
    if(on) lv_timer_resume(o->fast);
    else lv_timer_pause(o->fast);
}

static void fast_cb(lv_timer_t *t)
{
    orb_t *o = lv_timer_get_user_data(t);
    render(o, lv_tick_get());
    if(!needs_fast(o)) set_fast(o, false);
}

static void ambient_step(void *ctx, uint32_t now)
{
    orb_t *o = ctx;
    if(!needs_fast(o)) render(o, now);
}

static void kick(orb_t *o)
{
    if(needs_fast(o)) set_fast(o, true);
    else render(o, lv_tick_get());
}

/* overflow_visible only lets children draw within the parent's ext draw size, which is 0 by default: without
   this the glow is cut to the container's square and shows as a square behind the orb. */
static void ext_draw_cb(lv_event_t *e) { lv_event_set_ext_draw_size(e, GLOW_PAD); }

static void delete_cb(lv_event_t *e)
{
    orb_t *o = lv_event_get_user_data(e);
    cb_ambient_remove(ambient_step, o);
    set_fast(o, false);
    lv_timer_delete(o->fast);
    lv_draw_buf_destroy(o->buf);
    lv_free(o);
}

lv_obj_t *orb_create(lv_obj_t *parent, int32_t diameter)
{
    orb_t *o = lv_malloc_zeroed(sizeof(*o));
    LV_ASSERT_MALLOC(o);
    o->d = diameter;
    o->size = diameter + 2 * GLOW_PAD;
    o->act = NV_IDLE;
    o->from = o->to = k_pal[P_IDLE];
    o->last = lv_tick_get();
    o->buf = lv_draw_buf_create((uint32_t)o->size, (uint32_t)o->size, LV_COLOR_FORMAT_ARGB8888, 0);
    LV_ASSERT_MALLOC(o->buf);
    lv_draw_buf_clear(o->buf, NULL);

    lv_obj_t *cont = lv_obj_create(parent);
    lv_obj_remove_style_all(cont);
    lv_obj_set_size(cont, diameter, diameter);
    lv_obj_set_overflow_visible(cont, true);
    lv_obj_set_scrollable(cont, false);
    lv_obj_set_clickable(cont, true);
    lv_obj_set_user_data(cont, o);
    lv_obj_add_event_cb(cont, ext_draw_cb, LV_EVENT_REFR_EXT_DRAW_SIZE, NULL);
    lv_obj_refresh_ext_draw_size(cont);

    o->img = lv_image_create(cont);
    lv_obj_set_clickable(o->img, false);
    lv_image_set_src(o->img, o->buf);
    lv_obj_center(o->img);

    o->fast = lv_timer_create(fast_cb, ACTIVE_MS, o);
    lv_timer_pause(o->fast);
    cb_ambient_add(ambient_step, o);
    lv_obj_add_event_cb(cont, delete_cb, LV_EVENT_DELETE, o);
    render(o, o->last);
    return cont;
}

void orb_set_state(lv_obj_t *orb, nv_activity_t a, bool muted)
{
    orb_t *o = lv_obj_get_user_data(orb);
    if(!o || (o->act == a && o->muted == muted)) return;
    uint32_t now = lv_tick_get();
    if(o->blending) {   /* restart from the colours on screen right now */
        float k = (float)(now - o->blend_t0) / BLEND_MS;
        k = k > 1.0f ? 1.0f : k;
        o->from = palette_mix(&o->from, &o->to, k * k * (3.0f - 2.0f * k));
    }
    else {
        o->from = o->to;
    }
    o->act = a;
    o->muted = muted;
    o->to = k_pal[palette_for(a, muted)];
    o->blend_t0 = now;
    o->blending = true;
    kick(o);
}

void orb_set_level(lv_obj_t *orb, uint8_t level)
{
    orb_t *o = lv_obj_get_user_data(orb);
    if(o) o->level = level;
}

void orb_bloom(lv_obj_t *orb)
{
    orb_t *o = lv_obj_get_user_data(orb);
    if(!o || cb_reduce_motion()) return;
    o->bloom_t0 = lv_tick_get();
    o->blooming = true;
    kick(o);
}
