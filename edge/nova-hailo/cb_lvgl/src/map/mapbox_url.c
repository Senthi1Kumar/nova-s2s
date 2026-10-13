#include "map/mapbox_url.h"
#include <stdbool.h>
#include <string.h>

typedef struct {
    char *p;
    size_t cap, len;
    bool ok;
} sbuf_t;

static void put(sbuf_t *b, const char *s, size_t n)
{
    if(!b->ok) return;
    if(b->len + n + 1 > b->cap) {
        b->ok = false;
        return;
    }
    memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = '\0';
}

static void put_str(sbuf_t *b, const char *s) { put(b, s, strlen(s)); }

static void put_spaces_escaped(sbuf_t *b, const char *s)
{
    for(; *s; s++) {
        if(*s == ' ') put(b, "%20", 3);
        else put(b, s, 1);
    }
}

static int finish(sbuf_t *b, const char *token, bool has_query)
{
    put_str(b, has_query ? "&access_token=" : "?access_token=");
    put_str(b, token);
    return b->ok ? 1 : -1;
}

static bool is_mapbox_host(const char *host, size_t len)
{
    static const char root[] = "mapbox.com", sub[] = ".mapbox.com";
    if(len == sizeof root - 1 && strncmp(host, root, len) == 0) return true;
    return len > sizeof sub - 1 && strncmp(host + len - (sizeof sub - 1), sub, sizeof sub - 1) == 0;
}

int cb_mapbox_url(const char *url, const char *token, char *out, size_t cap)
{
    if(!url || !out || cap == 0) return -1;
    out[0] = '\0';
    sbuf_t b = { out, cap, 0, true };
    bool have_token = token && *token;

    if(strncmp(url, "https://", 8) == 0) {
        const char *host = url + 8, *slash = strchr(host, '/');
        size_t host_len = slash ? (size_t)(slash - host) : strlen(host);
        if(!is_mapbox_host(host, host_len) || strstr(url, "access_token=")) return 0;
        if(!have_token) return -1;
        put_str(&b, url);
        return finish(&b, token, strchr(url, '?') != NULL);
    }
    if(strncmp(url, "mapbox://", 9) != 0) return 0;
    if(!have_token) return -1;
    const char *p = url + 9;

    if(strncmp(p, "styles/", 7) == 0) {
        put_str(&b, "https://api.mapbox.com/styles/v1/");
        put_str(&b, p + 7);
        return finish(&b, token, strchr(p, '?') != NULL);
    }
    if(strncmp(p, "sprites/", 8) == 0) {   /* U/ID[@2x].ext: MapLibre has already appended the suffix */
        const char *user = p + 8, *slash = strchr(user, '/');
        if(!slash) return -1;
        const char *dot = strrchr(slash + 1, '.');
        if(!dot) return -1;
        const char *hidpi = strstr(slash + 1, "@2x");
        const char *id_end = hidpi && hidpi < dot ? hidpi : dot;
        put_str(&b, "https://api.mapbox.com/styles/v1/");
        put(&b, user, (size_t)(id_end - user));
        put_str(&b, "/sprite");
        put_str(&b, id_end);
        return finish(&b, token, false);
    }
    if(strncmp(p, "fonts/", 6) == 0) {
        put_str(&b, "https://api.mapbox.com/fonts/v1/");
        put_spaces_escaped(&b, p + 6);
        return finish(&b, token, false);
    }
    if(strncmp(p, "tiles/", 6) == 0) {
        put_str(&b, "https://api.mapbox.com/v4/");
        put_str(&b, p + 6);
        return finish(&b, token, strchr(p, '?') != NULL);
    }
    if(*p == '\0' || strchr(p, '/')) return -1;   /* TileJSON: mapbox://tileset[,tileset] */
    put_str(&b, "https://api.mapbox.com/v4/");
    put_str(&b, p);
    put_str(&b, ".json?secure");
    return finish(&b, token, true);
}
