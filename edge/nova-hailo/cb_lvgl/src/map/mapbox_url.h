#pragma once
#include <stddef.h>

/* Turns Mapbox URLs into authenticated https URLs, the way Mapbox GL JS normalises them:
     mapbox://styles/U/ID                  -> https://api.mapbox.com/styles/v1/U/ID
     mapbox://TILESET[,TILESET]            -> https://api.mapbox.com/v4/TILESETS.json?secure   (TileJSON)
     mapbox://sprites/U/ID[@2x].(json|png) -> https://api.mapbox.com/styles/v1/U/ID/sprite[@2x].EXT
     mapbox://fonts/U/STACK/RANGE.pbf      -> https://api.mapbox.com/fonts/v1/U/STACK/RANGE.pbf (spaces escaped)
     mapbox://tiles/PATH                   -> https://api.mapbox.com/v4/PATH
   each with access_token appended; an https URL on *.mapbox.com without a token gets one.
   Returns 1 and fills out when the URL was rewritten, 0 when it is not a Mapbox URL that needs a token
   (pass it through unchanged), -1 when it cannot be rewritten (no token, malformed, out too small). */
int cb_mapbox_url(const char *url, const char *token, char *out, size_t cap);
