#pragma once
#include <stdint.h>

/* The car on the map, as navigation apps draw it: a blue disc in a white ring with a white chevron pointing up
   (north, until the map's icon-rotate turns it to the heading), on a soft shadow. Rendered once, 4x4
   supersampled, as premultiplied RGBA8 for a MapLibre style image. */
#define MAP_PUCK_PX 56
void map_puck_render(uint8_t *rgba, int size);   /* size * size * 4 bytes */
