#include "map/route_geojson.h"
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    char *p;
    size_t cap, len;
    bool full;
} buf_t;

static void put(buf_t *b, const char *fmt, ...)
{
    if(b->full) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b->p + b->len, b->cap - b->len, fmt, ap);
    va_end(ap);
    if(n < 0 || (size_t)n >= b->cap - b->len) b->full = true;
    else b->len += (size_t)n;
}

static void put_str(buf_t *b, const char *s)   /* a JSON string, escaped */
{
    put(b, "\"");
    for(const unsigned char *c = (const unsigned char *)(s ? s : ""); *c && !b->full; c++) {
        if(*c == '"' || *c == '\\') put(b, "\\%c", *c);
        else if(*c < 0x20) put(b, "\\u%04x", *c);
        else put(b, "%c", *c);
    }
    put(b, "\"");
}

static size_t finish(const buf_t *b) { return b->full ? 0 : b->len; }

static char level(const char *congestion, int seg, int len)
{
    char c = seg < len ? congestion[seg] : 'u';
    return c == 'l' || c == 'm' || c == 'h' || c == 's' ? c : 'u';
}

size_t route_geojson_traffic(char *out, size_t cap, const double (*ll)[2], int n, const char *congestion)
{
    if(!out || cap == 0 || n < 2) return 0;
    buf_t b = { out, cap, 0, false };
    int len = congestion ? (int)strlen(congestion) : 0;
    put(&b, "{\"type\":\"FeatureCollection\",\"features\":[");
    for(int i = 0; i < n - 1;) {   /* segment k joins points k and k + 1 */
        char c = level(congestion, i, len);
        int j = i + 1;
        while(j < n - 1 && level(congestion, j, len) == c) j++;
        put(&b, "%s{\"type\":\"Feature\",\"properties\":{\"c\":\"%c\"},\"geometry\":{\"type\":\"LineString\","
                "\"coordinates\":[", i ? "," : "", c);
        for(int k = i; k <= j; k++) put(&b, "%s[%.6f,%.6f]", k > i ? "," : "", ll[k][0], ll[k][1]);
        put(&b, "]}}");
        i = j;
    }
    put(&b, "]}");
    return finish(&b);
}

static void point(buf_t *b, const route_stop_t *s, int num, bool dest, bool first)
{
    put(b, "%s{\"type\":\"Feature\",\"properties\":{\"n\":%d,\"dest\":%s,\"name\":", first ? "" : ",", num,
        dest ? "true" : "false");
    put_str(b, s->name);
    put(b, "},\"geometry\":{\"type\":\"Point\",\"coordinates\":[%.6f,%.6f]}}", s->lon, s->lat);
}

size_t route_geojson_stops(char *out, size_t cap, const route_stop_t *stops, int n, const route_stop_t *dest)
{
    if(!out || cap == 0) return 0;
    buf_t b = { out, cap, 0, false };
    put(&b, "{\"type\":\"FeatureCollection\",\"features\":[");
    for(int i = 0; i < n; i++) point(&b, &stops[i], i + 1, false, i == 0);
    if(dest) point(&b, dest, 0, true, n == 0);
    put(&b, "]}");
    return finish(&b);
}

size_t route_geojson_car(char *out, size_t cap, double lat, double lon, double heading)
{
    if(!out || cap == 0) return 0;
    buf_t b = { out, cap, 0, false };
    put(&b, "{\"type\":\"Feature\",\"properties\":{\"h\":%.1f},\"geometry\":{\"type\":\"Point\",\"coordinates\":"
            "[%.7f,%.7f]}}", heading, lon, lat);
    return finish(&b);
}
