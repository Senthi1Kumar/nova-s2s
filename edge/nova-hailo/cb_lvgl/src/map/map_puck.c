#include "map/map_puck.h"
#include <math.h>
#include <stdbool.h>
#include <stddef.h>

typedef struct { float x, y; } pt_t;

static float edge(pt_t a, pt_t b, pt_t p) { return (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x); }

static bool in_tri(pt_t a, pt_t b, pt_t c, pt_t p)
{
    float d1 = edge(a, b, p), d2 = edge(b, c, p), d3 = edge(c, a, p);
    return !((d1 < 0 || d2 < 0 || d3 < 0) && (d1 > 0 || d2 > 0 || d3 > 0));
}

static void over(float *acc, float r, float g, float b, float a)
{
    float k = 1.0f - a;
    acc[0] = r * a + acc[0] * k;
    acc[1] = g * a + acc[1] * k;
    acc[2] = b * a + acc[2] * k;
    acc[3] = a + acc[3] * k;
}

void map_puck_render(uint8_t *rgba, int size)
{
    const float S = (float)size, c = S / 2.0f;
    const float r_ring = 0.40f * S, r_disc = 0.33f * S, r_shadow = 0.44f * S;
    /* the chevron: tip, right base, notch, left base (two triangles about the concave notch) */
    const pt_t tip = { 0, -0.21f * S }, rb = { 0.15f * S, 0.16f * S }, notch = { 0, 0.07f * S }, lb = { -0.15f * S, 0.16f * S };
    for(int y = 0; y < size; y++)
        for(int x = 0; x < size; x++) {
            float ring = 0, disc = 0, arrow = 0;
            for(int sy = 0; sy < 4; sy++)
                for(int sx = 0; sx < 4; sx++) {
                    pt_t p = { (float)x + ((float)sx + 0.5f) / 4.0f - c, (float)y + ((float)sy + 0.5f) / 4.0f - c };
                    float d = sqrtf(p.x * p.x + p.y * p.y);
                    ring += d <= r_ring;
                    disc += d <= r_disc;
                    arrow += in_tri(tip, rb, notch, p) || in_tri(tip, notch, lb, p);
                }
            float px = (float)x + 0.5f - c, py = (float)y + 0.5f - c - 0.03f * S;
            float ds = sqrtf(px * px + py * py), q = 1.0f - ds / r_shadow;
            float shadow = ds >= r_shadow ? 0.0f : fminf(0.3f, 0.6f * q * q);
            float acc[4] = { 0, 0, 0, 0 };
            over(acc, 0, 0, 0, shadow);
            over(acc, 1, 1, 1, ring / 16.0f);
            over(acc, 0.17f, 0.50f, 1.0f, disc / 16.0f);   /* #2b80ff */
            over(acc, 1, 1, 1, arrow / 16.0f);
            uint8_t *o = rgba + ((size_t)y * (size_t)size + (size_t)x) * 4;
            for(int i = 0; i < 4; i++) o[i] = (uint8_t)(acc[i] * 255.0f + 0.5f);
        }
}
