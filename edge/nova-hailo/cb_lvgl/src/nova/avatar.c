#include "nova/avatar.h"
#include "nova/avatar_test.h"
#include "cb_theme.h"
#include <math.h>
#include <string.h>

#define FAST_MS 33
#define MAX_AVATARS 16
#define MAX_MASKS 24
#define PI_F 3.1415927f
#define EYE_SCALE 1.4f   /* eyes vs the head radius; the mockup's 1.0 reads too small at dock size, 2.0 reads startled */
#define DEG(rad) ((int32_t)lroundf((rad) * (1800.0f / PI_F)))   /* radians -> LVGL's 0.1 degree */

typedef struct { float w, h, gap, dy; } eye_shape_t;   /* the eye's size and spacing on a head, in R */
static const eye_shape_t k_shape[AV_HEAD_COUNT] = {
    [AV_HEAD_PEBBLE] = { 1.0f, 1.0f, 0.27f, 0.0f },   [AV_HEAD_CAPSULE] = { 0.9f, 0.9f, 0.33f, 0.0f },
    [AV_HEAD_DOME] = { 1.0f, 0.95f, 0.27f, 0.08f },   [AV_HEAD_DROP] = { 0.95f, 0.95f, 0.25f, 0.12f },
    [AV_HEAD_CLOUD] = { 0.95f, 0.9f, 0.26f, 0.05f },  [AV_HEAD_GEM] = { 0.9f, 0.9f, 0.26f, 0.0f },
    [AV_HEAD_VISOR] = { 0.95f, 1.0f, 0.30f, 0.0f },   [AV_HEAD_EYES] = { 1.1f, 1.1f, 0.30f, 0.0f },
};

typedef struct { int32_t x, y, w, h; } geo_t;

typedef struct {
    lv_obj_t *box, *body, *eye[2], *arch[2], *spark[2];
    int32_t size;
    float r;
    av_head_t head;
    av_model_t m;
    lv_timer_t *fast, *wake;
    bool running;
    geo_t body_g, eye_g[2], arch_g[2], spark_g[2];
    int32_t body_rot, body_sx, body_sy, body_px, body_py, arch_opa[2], spark_opa[2], eye_rot[2], arch_width[2], arch_half[2], spark_scale[2];
    uint32_t top_col, bot_col, edge_col, eye_col;
    lv_color_t body_mid, eye_now;
} avatar_t;

static avatar_t *s_all[MAX_AVATARS];
static av_head_t s_default_head = AV_HEAD_PEBBLE;
static av_mood_t s_default_mood = AV_CALM;
static bool s_warned_full;

static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
static float minf(float a, float b) { return a < b ? a : b; }

/* ---------- colour ---------- */

static lv_color_t hsl(float hue, float sat, float light)   /* degrees, percent, percent */
{
    float h = fmodf(hue, 360.0f);
    if(h < 0.0f) h += 360.0f;
    float s = clampf(sat, 0.0f, 100.0f) / 100.0f, l = clampf(light, 0.0f, 100.0f) / 100.0f;
    float a = s * minf(l, 1.0f - l), c[3];
    for(int i = 0; i < 3; i++) {
        float k = fmodf((float)i * 8.0f + h / 30.0f, 12.0f);
        c[i] = l - a * fmaxf(-1.0f, fminf(fminf(k - 3.0f, 9.0f - k), 1.0f));
    }
    return lv_color_make((uint8_t)(c[0] * 255.0f + 0.5f), (uint8_t)(c[1] * 255.0f + 0.5f), (uint8_t)(c[2] * 255.0f + 0.5f));
}

/* ---------- baked silhouettes (dome, drop, cloud, gem) ---------- */

/* Signed distances in pixels, negative inside. */
static float sd_rbox(float x, float y, float bx, float by, float r)
{
    float qx = fabsf(x) - bx + r, qy = fabsf(y) - by + r;
    float ox = qx > 0.0f ? qx : 0.0f, oy = qy > 0.0f ? qy : 0.0f;
    return sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0.0f) - r;
}

static float sd_circle(float x, float y, float cx, float cy, float r) { return hypotf(x - cx, y - cy) - r; }

static float sd_poly(const float *vx, const float *vy, int n, float x, float y)
{
    float d = (x - vx[0]) * (x - vx[0]) + (y - vy[0]) * (y - vy[0]), sign = 1.0f;
    for(int i = 0, j = n - 1; i < n; j = i++) {
        float ex = vx[j] - vx[i], ey = vy[j] - vy[i], wx = x - vx[i], wy = y - vy[i];
        float t = clampf((wx * ex + wy * ey) / (ex * ex + ey * ey), 0.0f, 1.0f);
        float bx = wx - ex * t, by = wy - ey * t;
        d = fminf(d, bx * bx + by * by);
        bool c1 = y >= vy[i], c2 = y < vy[j], c3 = ex * wy > ey * wx;
        if((c1 && c2 && c3) || (!c1 && !c2 && !c3)) sign = -sign;
    }
    return sign * sqrtf(d);
}

#define DROP_PTS 56
static void drop_outline(float R, float *px, float *py)
{
    const float oy = 0.12f * R;
    const float p0[2] = { 0.18f * R, -1.05f * R }, a1[2] = { 0.55f * R, -0.55f * R }, a2[2] = { 0.82f * R, oy - 0.30f * R },
                p1[2] = { 0.82f * R, oy }, b1[2] = { -0.82f * R, oy - 0.55f * R }, b2[2] = { -0.25f * R, -0.80f * R };
    int n = 0;
    for(int i = 0; i < 16; i++) {   /* start -> right edge */
        float t = (float)i / 16.0f, u = 1.0f - t;
        px[n] = u * u * u * p0[0] + 3 * u * u * t * a1[0] + 3 * u * t * t * a2[0] + t * t * t * p1[0];
        py[n++] = u * u * u * p0[1] + 3 * u * u * t * a1[1] + 3 * u * t * t * a2[1] + t * t * t * p1[1];
    }
    for(int i = 0; i < 24; i++) {   /* the lower half circle, right to left through the bottom */
        float a = PI_F * (float)i / 24.0f;
        px[n] = 0.82f * R * cosf(a);
        py[n++] = oy + 0.82f * R * sinf(a);
    }
    for(int i = 0; i < 16; i++) {   /* left edge -> start */
        float t = (float)i / 16.0f, u = 1.0f - t;
        px[n] = u * u * u * (-0.82f * R) + 3 * u * u * t * b1[0] + 3 * u * t * t * b2[0] + t * t * t * p0[0];
        py[n++] = u * u * u * oy + 3 * u * u * t * b1[1] + 3 * u * t * t * b2[1] + t * t * t * p0[1];
    }
}

static float head_sdf(av_head_t head, float R, float x, float y)   /* (x, y) from the head centre */
{
    switch(head) {
        case AV_HEAD_DOME: {
            float top = fmaxf(sd_circle(x, y, 0.0f, 0.05f * R, 0.95f * R), y - 0.05f * R);   /* the round top half */
            float seat = sd_rbox(x, y - 0.3f * R, 0.95f * R, 0.5f * R, 0.22f * R);
            return fminf(top, seat);
        }
        case AV_HEAD_DROP: {
            float px[DROP_PTS], py[DROP_PTS];
            drop_outline(R, px, py);
            return sd_poly(px, py, DROP_PTS, x, y);
        }
        case AV_HEAD_CLOUD: {
            static const float c[5][3] = { { -0.42f, -0.3f, 0.55f }, { 0.42f, -0.3f, 0.55f }, { -0.45f, 0.3f, 0.55f },
                                           { 0.45f, 0.3f, 0.55f }, { 0.0f, 0.0f, 0.7f } };
            float d = 1e9f;
            for(int i = 0; i < 5; i++) d = fminf(d, sd_circle(x, y, c[i][0] * R, c[i][1] * R, c[i][2] * R));
            return d;
        }
        case AV_HEAD_GEM: {
            float px[6], py[6];
            for(int k = 0; k < 6; k++) {
                float a = (float)k * PI_F / 3.0f - PI_F / 2.0f;
                px[k] = 0.82f * R * cosf(a);
                py[k] = 0.82f * R * sinf(a);
            }
            return sd_poly(px, py, 6, x, y) - 0.14f * R;
        }
        default: return 1e9f;
    }
}

typedef struct { av_head_t head; int32_t box; lv_draw_buf_t *buf; } mask_t;
static mask_t s_masks[MAX_MASKS];
static int s_n_masks;

static int32_t mask_side(float R) { return (int32_t)ceilf(2.5f * R) | 1; }   /* odd, so the centre is a pixel */

static lv_draw_buf_t *mask_for(av_head_t head, int32_t box, float R)
{
    for(int i = 0; i < s_n_masks; i++)
        if(s_masks[i].head == head && s_masks[i].box == box) return s_masks[i].buf;
    if(s_n_masks == MAX_MASKS) return NULL;
    int32_t m = mask_side(R);
    lv_draw_buf_t *buf = lv_draw_buf_create((uint32_t)m, (uint32_t)m, LV_COLOR_FORMAT_A8, 0);
    if(!buf) return NULL;
    uint32_t stride = buf->header.stride;
    float c = (float)m * 0.5f;
    for(int32_t y = 0; y < m; y++) {
        uint8_t *row = buf->data + (size_t)y * stride;
        for(int32_t x = 0; x < m; x++) {
            float d = head_sdf(head, R, (float)x + 0.5f - c, (float)y + 0.5f - c);
            row[x] = (uint8_t)(clampf(0.5f - d, 0.0f, 1.0f) * 255.0f + 0.5f);
        }
    }
    s_masks[s_n_masks++] = (mask_t){ head, box, buf };
    return buf;
}

/* The greeting glint: a thin four-point star (8 vertices, outer radius r, inner 0.3 r, from -90 degrees), baked once. */
#define STAR_SIDE 65
#define STAR_R 30.0f

static lv_draw_buf_t *star_mask(void)
{
    static lv_draw_buf_t *buf;
    if(buf) return buf;
    buf = lv_draw_buf_create(STAR_SIDE, STAR_SIDE, LV_COLOR_FORMAT_A8, 0);
    if(!buf) return NULL;
    float vx[8], vy[8];
    for(int k = 0; k < 8; k++) {
        float a = -PI_F * 0.5f + (float)k * PI_F * 0.25f, r = k & 1 ? 0.3f * STAR_R : STAR_R;
        vx[k] = r * cosf(a);
        vy[k] = r * sinf(a);
    }
    const int ss = 4;
    const float c = (float)STAR_SIDE * 0.5f;
    for(int32_t y = 0; y < STAR_SIDE; y++) {
        uint8_t *row = buf->data + (size_t)y * buf->header.stride;
        for(int32_t x = 0; x < STAR_SIDE; x++) {
            int hit = 0;
            for(int sy = 0; sy < ss; sy++)
                for(int sx = 0; sx < ss; sx++) {
                    float px = (float)x + ((float)sx + 0.5f) / (float)ss - c, py = (float)y + ((float)sy + 0.5f) / (float)ss - c;
                    bool in = false;
                    for(int a = 0, b = 7; a < 8; b = a++)
                        if((vy[a] > py) != (vy[b] > py) && px < (vx[b] - vx[a]) * (py - vy[a]) / (vy[b] - vy[a]) + vx[a]) in = !in;
                    hit += in;
                }
            row[x] = (uint8_t)(hit * 255 / (ss * ss));
        }
    }
    return buf;
}

/* ---------- parts ---------- */

static lv_obj_t *plain(lv_obj_t *parent)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_clickable(o, false);
    lv_obj_set_scrollable(o, false);
    return o;
}

static bool masked(av_head_t h) { return h == AV_HEAD_DOME || h == AV_HEAD_DROP || h == AV_HEAD_CLOUD || h == AV_HEAD_GEM; }

static void build_body(avatar_t *v)
{
    if(v->body) lv_obj_delete(v->body);
    v->body = NULL;
    v->body_g = (geo_t){ -1, -1, -1, -1 };
    v->top_col = v->bot_col = v->edge_col = 0xffffffffu;
    v->body_rot = 0;
    v->body_sx = v->body_sy = 256;
    v->body_px = v->body_py = -1;
    const float R = v->r;
    if(masked(v->head)) {
        lv_draw_buf_t *buf = mask_for(v->head, v->size, R);
        if(!buf) return;
        v->body = lv_image_create(v->box);
        lv_obj_set_clickable(v->body, false);
        lv_image_set_src(v->body, buf);
        lv_obj_set_style_image_recolor_opa(v->body, LV_OPA_COVER, 0);
        int32_t m = mask_side(R);
        lv_image_set_pivot(v->body, m / 2, m / 2 + (int32_t)lroundf(R));
    }
    else if(v->head != AV_HEAD_EYES) {
        lv_obj_t *o = plain(v->box);
        lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
        if(v->head == AV_HEAD_VISOR) {
            lv_obj_set_style_bg_color(o, lv_color_hex(0x0e131c), 0);
            lv_obj_set_style_border_width(o, 1, 0);
            lv_obj_set_style_border_opa(o, LV_OPA_70, 0);
            lv_obj_set_style_radius(o, (int32_t)lroundf(0.5f * R), 0);
        }
        else {
            lv_obj_set_style_bg_grad_dir(o, LV_GRAD_DIR_VER, 0);
            lv_obj_set_style_radius(o, v->head == AV_HEAD_CAPSULE ? LV_RADIUS_CIRCLE : (int32_t)lroundf(0.78f * R), 0);
        }
        v->body = o;
    }
    if(v->body) lv_obj_move_to_index(v->body, 0);
}

static void build_parts(avatar_t *v)
{
    build_body(v);
    for(int i = 0; i < 2; i++) {
        lv_obj_t *e = plain(v->box);
        lv_obj_set_style_bg_opa(e, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(e, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_transform_pivot_x(e, lv_pct(50), 0);
        lv_obj_set_style_transform_pivot_y(e, lv_pct(50), 0);
        v->eye[i] = e;

        lv_obj_t *a = lv_arc_create(v->box);
        lv_obj_set_clickable(a, false);
        lv_obj_set_scrollable(a, false);
        lv_obj_remove_style(a, NULL, LV_PART_KNOB);
        lv_obj_set_style_arc_opa(a, LV_OPA_TRANSP, LV_PART_MAIN);
        lv_obj_set_style_arc_rounded(a, true, LV_PART_INDICATOR);
        lv_obj_set_style_transform_pivot_x(a, lv_pct(50), 0);
        lv_obj_set_style_transform_pivot_y(a, lv_pct(50), 0);
        lv_arc_set_range(a, 0, 100);
        lv_arc_set_value(a, 100);
        lv_obj_set_hidden(a, true);
        v->arch[i] = a;

        lv_draw_buf_t *star = star_mask();
        lv_obj_t *s = NULL;
        if(star) {
            s = lv_image_create(v->box);
            lv_obj_set_clickable(s, false);
            lv_image_set_src(s, star);
            lv_image_set_pivot(s, STAR_SIDE / 2, STAR_SIDE / 2);
            lv_obj_set_style_image_recolor(s, hsl(45.0f, 85.0f, 82.0f), 0);
            lv_obj_set_style_image_recolor_opa(s, LV_OPA_COVER, 0);
            lv_obj_set_hidden(s, true);
        }
        v->spark[i] = s;
        v->eye_g[i] = v->arch_g[i] = v->spark_g[i] = (geo_t){ -1, -1, -1, -1 };
        v->eye_rot[i] = 0;
        v->arch_width[i] = v->arch_half[i] = v->arch_opa[i] = v->spark_opa[i] = v->spark_scale[i] = -1;
    }
    v->eye_col = 0xffffffffu;
}

static void put(lv_obj_t *o, geo_t *g, int32_t x, int32_t y, int32_t w, int32_t h)
{
    if(x != g->x || y != g->y) lv_obj_set_pos(o, x, y);
    if(w != g->w || h != g->h) lv_obj_set_size(o, w, h);
    *g = (geo_t){ x, y, w, h };
}

static void show(lv_obj_t *o, bool on)
{
    if(on == !lv_obj_is_hidden(o)) return;
    lv_obj_set_hidden(o, !on);
}

/* ---------- one frame ---------- */

/* Lean in 0.1 degrees; a hair of it is 0, so a still face needs no transform layer. */
static int32_t snap_lean(float lean) { return fabsf(lean) < 0.005f ? 0 : DEG(lean); }

static void set_body(avatar_t *v, float cx, float cy, lv_color_t top, lv_color_t bot, lv_color_t edge,
                     const av_frame_t *f)
{
    if(!v->body) return;
    const float R = v->r;
    uint32_t t = lv_color_to_u32(top), b = lv_color_to_u32(bot), e = lv_color_to_u32(edge);
    int32_t rot = snap_lean(f->p.lean), sx = (int32_t)lroundf(256.0f * f->swell), sy = (int32_t)lroundf(256.0f * f->swell * f->nod);
    if(masked(v->head)) {
        int32_t m = mask_side(R);
        put(v->body, &v->body_g, (int32_t)lroundf(cx) - m / 2, (int32_t)lroundf(cy) - m / 2, m, m);
        if(t != v->top_col || b != v->bot_col) {
            lv_obj_set_style_image_recolor(v->body, lv_color_mix(top, bot, 128), 0);
            v->top_col = t;
            v->bot_col = b;
        }
        if(rot != v->body_rot) lv_image_set_rotation(v->body, rot);
        if(sx != v->body_sx) lv_image_set_scale_x(v->body, (uint32_t)sx);
        if(sy != v->body_sy) lv_image_set_scale_y(v->body, (uint32_t)sy);
    }
    else {
        float hw, hh;   /* half sizes, in R */
        switch(v->head) {
            case AV_HEAD_CAPSULE: hw = 1.05f; hh = 0.68f; break;
            case AV_HEAD_VISOR: hw = 1.0f; hh = 0.72f; break;
            default: hw = 0.93f; hh = 0.90f; break;
        }
        /* swell and nod resize the body about its seat (bottom centre) instead of scaling it */
        float kx = f->swell, ky = f->swell * f->nod, seat = cy + R;
        int32_t w = (int32_t)lroundf(2.0f * hw * R * kx), h = (int32_t)lroundf(2.0f * hh * R * ky);
        float pivy = (1.0f + hh) * R * ky;
        put(v->body, &v->body_g, (int32_t)lroundf(cx - hw * R * kx), (int32_t)lroundf(seat - pivy), w, h);
        if(rot) {   /* the rotation pivot follows the seat */
            int32_t px = w / 2, py = (int32_t)lroundf(pivy);
            if(px != v->body_px) lv_obj_set_style_transform_pivot_x(v->body, px, 0);
            if(py != v->body_py) lv_obj_set_style_transform_pivot_y(v->body, py, 0);
            v->body_px = px;
            v->body_py = py;
        }
        if(t != v->top_col || b != v->bot_col) {
            if(v->head != AV_HEAD_VISOR) {
                lv_obj_set_style_bg_color(v->body, top, 0);
                lv_obj_set_style_bg_grad_color(v->body, bot, 0);
            }
            v->top_col = t;
            v->bot_col = b;
        }
        if(v->head == AV_HEAD_VISOR && e != v->edge_col) {
            lv_obj_set_style_border_color(v->body, edge, 0);
            v->edge_col = e;
        }
        if(rot != v->body_rot) lv_obj_set_style_transform_rotation(v->body, rot, 0);
    }
    v->body_rot = rot;
    v->body_sx = sx;
    v->body_sy = sy;
}

static void set_eye(avatar_t *v, int i, float ex, float ey, float scale, float tilt, const av_frame_t *f,
                    const eye_shape_t *sh, lv_color_t col)
{
    const float R = v->r, head_d = EYE_SCALE * R;
    const av_pose_t *p = &f->p;
    av_eye_t mo = av_eye_morph(p->eyeH, p->happy);
    float w = p->eyeW * head_d * scale * sh->w, h = p->eyeH * head_d * scale * sh->h;
    float hh = mo.pill_h * head_d * scale * sh->h;
    float top = ey - hh * 0.5f + p->lidT * hh, bot = ey + hh * 0.5f - p->lidB * hh;
    float vh = fmaxf(bot - top, 0.035f * R);
    float ecy = (top + bot) * 0.5f;

    int32_t rot = snap_lean(p->lean) + (fabsf(tilt) < 0.01f ? 0 : DEG(tilt));
    bool pill = mo.pill_opa > 0.0f, arch = mo.arch_opa > 0.0f;
    show(v->eye[i], pill);
    show(v->arch[i], arch);
    uint32_t c32 = lv_color_to_u32(col);
    if(c32 != v->eye_col) {
        lv_obj_set_style_bg_color(v->eye[i], col, 0);
        lv_obj_set_style_arc_color(v->arch[i], col, LV_PART_INDICATOR);
    }
    if(rot != v->eye_rot[i]) {
        lv_obj_set_style_transform_rotation(v->eye[i], rot, 0);
        lv_obj_set_style_transform_rotation(v->arch[i], rot, 0);
        v->eye_rot[i] = rot;
    }
    if(pill) {
        put(v->eye[i], &v->eye_g[i], (int32_t)lroundf(ex - w * 0.5f), (int32_t)lroundf(ecy - vh * 0.5f),
            (int32_t)lroundf(fmaxf(w, 1.0f)), (int32_t)lroundf(fmaxf(vh, 1.0f)));
    }
    if(arch) {
        float rad = 0.62f * w, lw = 0.48f * w * (0.55f + 0.45f * mo.arch_rise), size = 2.0f * (rad + 0.48f * w * 0.5f);
        int32_t sz = (int32_t)lroundf(size);
        put(v->arch[i], &v->arch_g[i], (int32_t)lroundf(ex - size * 0.5f), (int32_t)lroundf(ey + 0.2f * h - size * 0.5f), sz, sz);
        int32_t lwi = (int32_t)lroundf(fmaxf(lw, 1.0f)), half = (int32_t)lroundf(61.2f * (0.4f + 0.6f * mo.arch_rise));
        if(lwi != v->arch_width[i]) {
            lv_obj_set_style_arc_width(v->arch[i], lwi, LV_PART_INDICATOR);
            v->arch_width[i] = lwi;
        }
        if(half != v->arch_half[i]) {
            lv_arc_set_bg_angles(v->arch[i], (lv_value_precise_t)(270 - half), (lv_value_precise_t)(270 + half));
            v->arch_half[i] = half;
        }
        int32_t ao = (int32_t)(255.0f * mo.arch_opa);
        if(ao != v->arch_opa[i]) {
            lv_obj_set_style_arc_opa(v->arch[i], (lv_opa_t)ao, LV_PART_INDICATOR);
            v->arch_opa[i] = ao;
        }
    }
}

static void render(avatar_t *v, uint32_t now)
{
    av_model_set_reduce_motion(&v->m, cb_reduce_motion());
    av_frame_t f = av_model_step(&v->m, now);
    const av_pose_t *p = &f.p;
    const float R = v->r, s = (float)v->size, cx = s * 0.5f, cy = 0.53f * s + f.hop * R;
    const eye_shape_t *sh = &k_shape[v->head];
    bool tinted = v->head == AV_HEAD_VISOR || v->head == AV_HEAD_EYES;

    lv_color_t top = hsl(p->hue, p->sat, 64.0f), bot = hsl(p->hue, p->sat, 52.0f);
    lv_color_t edge = hsl(p->hue, p->sat, 45.0f), eye = lv_color_hex(0xfbfdff);
    if(tinted) {   /* a muted (grey) face gets no extra colour */
        float boost = 30.0f * clampf((p->sat - 5.0f) / 10.0f, 0.0f, 1.0f);
        eye = hsl(p->hue, minf(p->sat + boost, 100.0f), v->head == AV_HEAD_EYES ? 76.0f : 74.0f);
    }
    v->body_mid = hsl(p->hue, p->sat, 58.0f);
    v->eye_now = eye;
    set_body(v, cx, cy, top, bot, edge, &f);

    /* eye centres: the face's own layout, then carried by the body's lean and swell about its seat */
    float sx = f.swell, sy = f.swell * f.nod, cs = cosf(p->lean), sn = sinf(p->lean);
    float px = cx, py = cy + R;
    for(int i = 0; i < 2; i++) {
        float side = i ? 1.0f : -1.0f;
        float scale = i ? 1.0f + p->asym * 0.35f : 1.0f - p->asym * 0.45f;
        float ex = cx + (side * sh->gap + p->lookX) * R, ey = cy + (p->lookY + sh->dy) * R;
        float dx = (ex - px) * sx, dy = (ey - py) * sy;
        float rx = px + dx * cs - dy * sn, ry = py + dx * sn + dy * cs;
        set_eye(v, i, rx, ry, scale * sx, side * -p->tilt, &f, sh, eye);
    }
    v->eye_col = lv_color_to_u32(eye);

    /* greeting sparkles: small four-point stars, twinkling */
    static const struct { float dx, dy, s, ph; } k_spark[2] = { { -0.62f, -1.12f, 0.10f, 0.0f }, { 0.70f, -1.02f, 0.07f, 1.9f } };
    float t = (float)(now % 600000u) / 1000.0f;
    for(int i = 0; i < 2; i++) {
        float tw = 0.55f + 0.45f * sinf(4.0f * t + k_spark[i].ph);
        float opa = clampf(p->spark, 0.0f, 1.0f) * tw;
        bool on = opa > 0.02f;
        if(!v->spark[i]) continue;
        show(v->spark[i], on);
        if(!on) continue;
        float r = k_spark[i].s * R * EYE_SCALE * (0.75f + 0.25f * tw);
        float sx = fminf(fmaxf(cx + k_spark[i].dx * R, r), s - r), sy = fminf(fmaxf(cy + k_spark[i].dy * R, r), s - r);
        put(v->spark[i], &v->spark_g[i], (int32_t)lroundf(sx) - STAR_SIDE / 2, (int32_t)lroundf(sy) - STAR_SIDE / 2,
            STAR_SIDE, STAR_SIDE);
        int32_t sc = (int32_t)lroundf(256.0f * r / STAR_R);
        if(sc < 1) sc = 1;
        if(sc != v->spark_scale[i]) {
            lv_image_set_scale(v->spark[i], (uint32_t)sc);
            v->spark_scale[i] = sc;
        }
        int32_t so = (int32_t)(255.0f * opa);
        if(so != v->spark_opa[i]) {
            lv_obj_set_style_image_opa(v->spark[i], (lv_opa_t)so, 0);
            v->spark_opa[i] = so;
        }
    }
}

/* ---------- timers ---------- */

static void fast_cb(lv_timer_t *t);

static void run_fast(avatar_t *v, bool on)
{
    if(on) lv_timer_resume(v->fast);
    else lv_timer_pause(v->fast);
    v->running = on;
}

static void arm_wake(avatar_t *v, uint32_t now)
{
    if(cb_reduce_motion()) return;
    int32_t gap = (int32_t)(av_model_next_wake(&v->m) - now);
    lv_timer_set_period(v->wake, gap < 1 ? 1u : (uint32_t)gap);
    lv_timer_reset(v->wake);
    lv_timer_resume(v->wake);
}

static void fast_cb(lv_timer_t *t)
{
    avatar_t *v = lv_timer_get_user_data(t);
    uint32_t now = lv_tick_get();
    render(v, now);
    if(av_model_settled(&v->m)) {
        run_fast(v, false);
        arm_wake(v, now);
    }
}

static void wake_cb(lv_timer_t *t)
{
    avatar_t *v = lv_timer_get_user_data(t);
    lv_timer_pause(t);
    run_fast(v, true);
    render(v, lv_tick_get());
}

static void kick(avatar_t *v)
{
    lv_timer_pause(v->wake);
    run_fast(v, true);
    render(v, lv_tick_get());
}

/* ---------- public ---------- */

static void delete_cb(lv_event_t *e)
{
    avatar_t *v = lv_event_get_user_data(e);
    for(int i = 0; i < MAX_AVATARS; i++)
        if(s_all[i] == v) s_all[i] = NULL;
    lv_timer_delete(v->fast);
    lv_timer_delete(v->wake);
    lv_free(v);
}

lv_obj_t *avatar_create(lv_obj_t *parent, int32_t box)
{
    avatar_t *v = lv_malloc_zeroed(sizeof(*v));
    LV_ASSERT_MALLOC(v);
    v->size = box;
    v->r = 0.27f * (float)box;
    v->head = s_default_head;
    av_model_init(&v->m, (uint32_t)(uintptr_t)v, lv_tick_get());
    av_model_set_mood(&v->m, s_default_mood);
    v->m.pose = av_model_target(&v->m);

    v->box = plain(parent);
    lv_obj_set_size(v->box, box, box);
    lv_obj_set_user_data(v->box, v);
    build_parts(v);

    v->fast = lv_timer_create(fast_cb, FAST_MS, v);
    v->wake = lv_timer_create(wake_cb, 1000, v);
    lv_timer_set_auto_delete(v->wake, false);
    lv_timer_pause(v->wake);
    lv_obj_add_event_cb(v->box, delete_cb, LV_EVENT_DELETE, v);
    for(int i = 0; i < MAX_AVATARS; i++)
        if(!s_all[i]) {
            s_all[i] = v;
            break;
        }
        else if(i == MAX_AVATARS - 1 && !s_warned_full) {
            s_warned_full = true;
            LV_LOG_WARN("avatar registry full (%d): the extra avatar will miss pushes", MAX_AVATARS);
        }
    run_fast(v, true);
    render(v, lv_tick_get());
    return v->box;
}

static avatar_t *get(lv_obj_t *av) { return av ? lv_obj_get_user_data(av) : NULL; }

void avatar_set_state(lv_obj_t *av, nv_activity_t a, bool muted)
{
    avatar_t *v = get(av);
    if(!v) return;
    av_model_set_state(&v->m, av_state_for(a, muted), lv_tick_get());
    kick(v);
}

void avatar_set_av_state(lv_obj_t *av, av_state_t s)
{
    avatar_t *v = get(av);
    if(!v) return;
    av_model_set_state(&v->m, s, lv_tick_get());
    kick(v);
}

void avatar_set_mood(lv_obj_t *av, av_mood_t mood)
{
    avatar_t *v = get(av);
    if(!v) return;
    av_model_set_mood(&v->m, mood);
    kick(v);
}

void avatar_set_head(lv_obj_t *av, av_head_t head)
{
    avatar_t *v = get(av);
    if(!v || head >= AV_HEAD_COUNT) return;
    if(head != v->head) {
        v->head = head;
        build_body(v);
        v->eye_col = 0xffffffffu;   /* the eyes change colour with the head */
    }
    kick(v);
}

void avatar_set_level(lv_obj_t *av, bool nova, uint8_t level)
{
    avatar_t *v = get(av);
    if(!v) return;
    av_model_set_level(&v->m, nova, (float)level / 255.0f);
    kick(v);
}

void avatar_set_duck(lv_obj_t *av, float gain)
{
    avatar_t *v = get(av);
    if(!v) return;
    av_model_set_duck(&v->m, gain);
    kick(v);
}

void avatar_greet(lv_obj_t *av)
{
    avatar_t *v = get(av);
    if(!v || cb_reduce_motion()) return;
    av_model_set_state(&v->m, AV_ST_GREETING, lv_tick_get());
    kick(v);
}

bool avatar_timer_running(lv_obj_t *av)
{
    avatar_t *v = get(av);
    return v && v->running;
}

void avatar_set_default_head(av_head_t head)
{
    if(head < AV_HEAD_COUNT) s_default_head = head;
}

void avatar_set_default_mood(av_mood_t mood)
{
    if(mood < AV_MOOD_COUNT) s_default_mood = mood;
}

void avatar_for_each(void (*fn)(lv_obj_t *av, void *ud), void *ud)
{
    for(int i = 0; i < MAX_AVATARS; i++)
        if(s_all[i]) fn(s_all[i]->box, ud);
}

av_head_t avatar_head(lv_obj_t *av)
{
    avatar_t *v = get(av);
    return v ? v->head : AV_HEAD_COUNT;
}

const av_model_t *avatar_model(lv_obj_t *av)
{
    avatar_t *v = get(av);
    return v ? &v->m : NULL;
}

lv_color_t avatar_body_color(lv_obj_t *av)
{
    avatar_t *v = get(av);
    return v ? v->body_mid : lv_color_black();
}

lv_color_t avatar_eye_color(lv_obj_t *av)
{
    avatar_t *v = get(av);
    return v ? v->eye_now : lv_color_black();
}
