#include "cb_test.h"
#include "map/map_puck.h"
#include <stdbool.h>

static uint8_t s_px[MAP_PUCK_PX * MAP_PUCK_PX * 4];

static const uint8_t *at(float dx, float dy)   /* offset from the centre, in puck sizes */
{
    int x = (int)(MAP_PUCK_PX / 2.0f + dx * MAP_PUCK_PX), y = (int)(MAP_PUCK_PX / 2.0f + dy * MAP_PUCK_PX);
    return s_px + ((size_t)y * MAP_PUCK_PX + (size_t)x) * 4;
}

static bool white(const uint8_t *p) { return p[0] > 230 && p[1] > 230 && p[2] > 230 && p[3] > 240; }
static bool blue(const uint8_t *p) { return p[2] > 200 && p[0] < 120 && p[3] > 240; }

static void shape(void) {
    map_puck_render(s_px, MAP_PUCK_PX);
    CHECK_EQ(s_px[3], 0);                 /* corner: transparent */
    CHECK(white(at(0.0f, -0.05f)));       /* the arrow's body */
    CHECK(blue(at(0.0f, 0.12f)));         /* the notch under the arrow: a chevron, not a triangle */
    CHECK(blue(at(0.0f, -0.28f)));        /* the disc beyond the tip */
    CHECK(white(at(0.365f, 0.0f)));       /* the white ring */
    CHECK(blue(at(-0.25f, 0.0f)));
}

static void premultiplied(void) {
    int bad = 0;
    for(int i = 0; i < MAP_PUCK_PX * MAP_PUCK_PX; i++) {
        const uint8_t *p = s_px + i * 4;
        if(p[0] > p[3] || p[1] > p[3] || p[2] > p[3]) bad++;
    }
    CHECK_EQ(bad, 0);
}

int main(void) {
    RUN(shape); RUN(premultiplied);
    DONE();
}
