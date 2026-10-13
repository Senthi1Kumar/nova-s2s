#include "live/json.h"
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *p, *end;
    cbj_t *nodes;
    size_t n, cap;
    char *str;
    size_t slen;
    int depth;
} parser_t;

#define MAX_DEPTH 64

static void ws(parser_t *ps)
{
    while(ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r')) ps->p++;
}

static cbj_t *node(parser_t *ps, cbj_type_t t)
{
    if(ps->n >= ps->cap) return NULL;
    cbj_t *n = &ps->nodes[ps->n++];
    memset(n, 0, sizeof *n);
    n->type = t;
    return n;
}

static int hex4(const char *p)
{
    int v = 0;
    for(int i = 0; i < 4; i++) {
        char c = p[i];
        v <<= 4;
        if(c >= '0' && c <= '9') v |= c - '0';
        else if(c >= 'a' && c <= 'f') v |= c - 'a' + 10;
        else if(c >= 'A' && c <= 'F') v |= c - 'A' + 10;
        else return -1;
    }
    return v;
}

static void put_utf8(parser_t *ps, unsigned cp)
{
    char *o = ps->str + ps->slen;
    if(cp < 0x80) {
        o[0] = (char)cp;
        ps->slen += 1;
    }
    else if(cp < 0x800) {
        o[0] = (char)(0xC0 | (cp >> 6));
        o[1] = (char)(0x80 | (cp & 0x3F));
        ps->slen += 2;
    }
    else if(cp < 0x10000) {
        o[0] = (char)(0xE0 | (cp >> 12));
        o[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        o[2] = (char)(0x80 | (cp & 0x3F));
        ps->slen += 3;
    }
    else {
        o[0] = (char)(0xF0 | (cp >> 18));
        o[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        o[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        o[3] = (char)(0x80 | (cp & 0x3F));
        ps->slen += 4;
    }
}

/* Unescapes into the string area; an escape never yields more bytes than it occupies (\uXXXX = 6 -> <= 3,
   a surrogate pair = 12 -> 4), so an area sized to the input always suffices. */
static const char *string(parser_t *ps)
{
    if(ps->p >= ps->end || *ps->p != '"') return NULL;
    ps->p++;
    const char *out = ps->str + ps->slen;
    while(ps->p < ps->end && *ps->p != '"') {
        unsigned char c = (unsigned char)*ps->p;
        if(c < 0x20) return NULL;
        if(c != '\\') {
            ps->str[ps->slen++] = (char)c;
            ps->p++;
            continue;
        }
        if(ps->end - ps->p < 2) return NULL;
        char e = ps->p[1];
        ps->p += 2;
        switch(e) {
            case '"': ps->str[ps->slen++] = '"'; break;
            case '\\': ps->str[ps->slen++] = '\\'; break;
            case '/': ps->str[ps->slen++] = '/'; break;
            case 'b': ps->str[ps->slen++] = '\b'; break;
            case 'f': ps->str[ps->slen++] = '\f'; break;
            case 'n': ps->str[ps->slen++] = '\n'; break;
            case 'r': ps->str[ps->slen++] = '\r'; break;
            case 't': ps->str[ps->slen++] = '\t'; break;
            case 'u': {
                if(ps->end - ps->p < 4) return NULL;
                int cp = hex4(ps->p);
                if(cp < 0) return NULL;
                ps->p += 4;
                if(cp >= 0xD800 && cp <= 0xDBFF && ps->end - ps->p >= 6 && ps->p[0] == '\\' && ps->p[1] == 'u') {
                    int lo = hex4(ps->p + 2);
                    if(lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        ps->p += 6;
                    }
                }
                put_utf8(ps, (unsigned)cp);
                break;
            }
            default: return NULL;
        }
    }
    if(ps->p >= ps->end) return NULL;
    ps->p++;
    ps->str[ps->slen++] = '\0';
    return out;
}

static bool literal(parser_t *ps, const char *w)
{
    size_t n = strlen(w);
    if((size_t)(ps->end - ps->p) < n || strncmp(ps->p, w, n) != 0) return false;
    ps->p += n;
    return true;
}

static cbj_t *value(parser_t *ps);

static cbj_t *container(parser_t *ps, bool obj)
{
    if(++ps->depth > MAX_DEPTH) return NULL;
    cbj_t *c = node(ps, obj ? CBJ_OBJ : CBJ_ARR);
    if(!c) return NULL;
    char close = obj ? '}' : ']';
    ps->p++;
    ws(ps);
    if(ps->p < ps->end && *ps->p == close) {
        ps->p++;
        ps->depth--;
        return c;
    }
    cbj_t *last = NULL;
    for(;;) {
        const char *key = NULL;
        ws(ps);
        if(obj) {
            key = string(ps);
            if(!key) return NULL;
            ws(ps);
            if(ps->p >= ps->end || *ps->p != ':') return NULL;
            ps->p++;
        }
        cbj_t *v = value(ps);
        if(!v) return NULL;
        v->key = key;
        if(last) last->next = v;
        else c->child = v;
        last = v;
        c->count++;
        ws(ps);
        if(ps->p >= ps->end) return NULL;
        if(*ps->p == ',') {
            ps->p++;
            continue;
        }
        if(*ps->p != close) return NULL;
        ps->p++;
        ps->depth--;
        return c;
    }
}

static cbj_t *value(parser_t *ps)
{
    ws(ps);
    if(ps->p >= ps->end) return NULL;
    char c = *ps->p;
    if(c == '{' || c == '[') return container(ps, c == '{');
    if(c == '"') {
        cbj_t *n = node(ps, CBJ_STR);
        if(!n || !(n->s = string(ps))) return NULL;
        return n;
    }
    if(c == 't' || c == 'f') {
        cbj_t *n = node(ps, CBJ_BOOL);
        if(!n) return NULL;
        n->b = c == 't';
        return literal(ps, n->b ? "true" : "false") ? n : NULL;
    }
    if(c == 'n') {
        cbj_t *n = node(ps, CBJ_NULL);
        return n && literal(ps, "null") ? n : NULL;
    }
    if(c == '-' || (c >= '0' && c <= '9')) {
        char buf[64];
        size_t k = 0;
        while(ps->p + k < ps->end && k < sizeof buf - 1 && ps->p[k] && strchr("+-0123456789.eE", ps->p[k])) {
            buf[k] = ps->p[k];
            k++;
        }
        buf[k] = '\0';
        char *e;
        double d = strtod(buf, &e);
        if(e == buf || (size_t)(e - buf) != k) return NULL;
        cbj_t *n = node(ps, CBJ_NUM);
        if(!n) return NULL;
        n->n = d;
        ps->p += k;
        return n;
    }
    return NULL;
}

cbj_t *cbj_parse(const char *text, size_t len)
{
    if(!text || len == 0) return NULL;
    size_t cap = len / 2 + 2;   /* each value after the first needs a separator and a character */
    char *block = malloc(cap * sizeof(cbj_t) + len + 1);
    if(!block) return NULL;
    parser_t ps = { text, text + len, (cbj_t *)(void *)block, 0, cap, block + cap * sizeof(cbj_t), 0, 0 };
    cbj_t *root = value(&ps);
    ws(&ps);
    if(!root || ps.p != ps.end) {
        free(block);
        return NULL;
    }
    return root;   /* the first node starts the block */
}

void cbj_free(cbj_t *root) { free(root); }

const cbj_t *cbj_get(const cbj_t *obj, const char *key)
{
    if(!obj || obj->type != CBJ_OBJ) return NULL;
    for(const cbj_t *c = obj->child; c; c = c->next)
        if(c->key && strcmp(c->key, key) == 0) return c;
    return NULL;
}

const cbj_t *cbj_at(const cbj_t *arr, int i)
{
    if(!arr || (arr->type != CBJ_ARR && arr->type != CBJ_OBJ) || i < 0) return NULL;
    const cbj_t *c = arr->child;
    while(c && i--) c = c->next;
    return c;
}

const char *cbj_str(const cbj_t *n, const char *def) { return n && n->type == CBJ_STR ? n->s : def; }
double cbj_num(const cbj_t *n, double def) { return n && n->type == CBJ_NUM ? n->n : def; }
bool cbj_bool(const cbj_t *n, bool def) { return n && n->type == CBJ_BOOL ? n->b : def; }
