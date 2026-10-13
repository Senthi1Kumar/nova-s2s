#include "cb_test.h"
#include "map/mapbox_url.h"
#include <string.h>

/* mapbox:// and api.mapbox.com URLs become authenticated https URLs, the way Mapbox GL JS normalises them;
   everything else passes through untouched. */

static char out[512];

static void expect(const char *url, const char *want)
{
    int r = cb_mapbox_url(url, "T", out, sizeof out);
    if(r != 1 || strcmp(out, want) != 0) fprintf(stderr, "url %s -> [%d] %s\n  want %s\n", url, r, out, want);
    CHECK_EQ(r, 1);
    CHECK(strcmp(out, want) == 0);
}

static void styles(void) { expect("mapbox://styles/mapbox/dark-v11", "https://api.mapbox.com/styles/v1/mapbox/dark-v11?access_token=T"); }

static void tilejson_sources(void)
{
    expect("mapbox://mapbox.mapbox-streets-v8,mapbox.mapbox-terrain-v2",
           "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8,mapbox.mapbox-terrain-v2.json?secure&access_token=T");
}

static void sprites(void)
{
    expect("mapbox://sprites/mapbox/dark-v11@2x.json", "https://api.mapbox.com/styles/v1/mapbox/dark-v11/sprite@2x.json?access_token=T");
    expect("mapbox://sprites/mapbox/dark-v11.png", "https://api.mapbox.com/styles/v1/mapbox/dark-v11/sprite.png?access_token=T");
}

static void glyphs(void)
{
    expect("mapbox://fonts/mapbox/DIN Pro Medium,Arial Unicode MS Regular/0-255.pbf",
           "https://api.mapbox.com/fonts/v1/mapbox/DIN%20Pro%20Medium,Arial%20Unicode%20MS%20Regular/0-255.pbf?access_token=T");
}

static void tiles(void)
{
    expect("mapbox://tiles/mapbox.mapbox-streets-v8/14/2620/6332.vector.pbf",
           "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/14/2620/6332.vector.pbf?access_token=T");
}

static void https_without_token_gets_one(void)
{
    expect("https://api.mapbox.com/v4/x/1/2/3.vector.pbf", "https://api.mapbox.com/v4/x/1/2/3.vector.pbf?access_token=T");
    expect("https://api.mapbox.com/v4/x.json?secure", "https://api.mapbox.com/v4/x.json?secure&access_token=T");
}

static void pass_through(void)
{
    CHECK_EQ(cb_mapbox_url("https://a.tiles.mapbox.com/v4/x/1/2/3.vector.pbf?access_token=pk.abc", "T", out, sizeof out), 0);
    CHECK_EQ(cb_mapbox_url("https://tiles.openfreemap.org/styles/liberty", "T", out, sizeof out), 0);
    CHECK_EQ(cb_mapbox_url("file:///tmp/style.json", "T", out, sizeof out), 0);
}

static void errors(void)
{
    CHECK_EQ(cb_mapbox_url("mapbox://styles/mapbox/dark-v11", NULL, out, sizeof out), -1);   /* no token */
    CHECK_EQ(cb_mapbox_url("mapbox://styles/mapbox/dark-v11", "", out, sizeof out), -1);
    CHECK_EQ(cb_mapbox_url("mapbox://styles/mapbox/dark-v11", "T", out, 20), -1);         /* too small */
    CHECK_EQ(cb_mapbox_url("mapbox://sprites/mapbox", "T", out, sizeof out), -1);          /* malformed */
}

int main(void)
{
    RUN(styles);
    RUN(tilejson_sources);
    RUN(sprites);
    RUN(glyphs);
    RUN(tiles);
    RUN(https_without_token_gets_one);
    RUN(pass_through);
    RUN(errors);
    DONE();
}
