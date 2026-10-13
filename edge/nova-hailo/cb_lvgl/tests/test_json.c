#include "live/json.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if(!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while(0)

static void frame(void)
{
    const char *t = "{\"type\": \"nova.vehicle\", \"seq\": 7, \"changed\": {\"Vehicle.Speed\": 42.5, "
                    "\"Vehicle.Private.Nova.Media.State\": \"playing\", \"X.On\": true, \"X.Null\": null},"
                    " \"list\": [1, -2.5e1, \"a\\\"b\\\\c\\n\", [], {}]}";
    cbj_t *r = cbj_parse(t, strlen(t));
    CHECK(r && r->type == CBJ_OBJ);
    CHECK(!strcmp(cbj_str(cbj_get(r, "type"), ""), "nova.vehicle"));
    CHECK(cbj_num(cbj_get(r, "seq"), -1) == 7);
    const cbj_t *ch = cbj_get(r, "changed");
    CHECK(ch && ch->type == CBJ_OBJ && ch->count == 4);
    CHECK(cbj_num(cbj_get(ch, "Vehicle.Speed"), 0) == 42.5);
    CHECK(cbj_bool(cbj_get(ch, "X.On"), false));
    CHECK(cbj_get(ch, "X.Null")->type == CBJ_NULL);
    int n = 0;
    for(const cbj_t *c = ch->child; c; c = c->next) n++;
    CHECK(n == 4);
    const cbj_t *l = cbj_get(r, "list");
    CHECK(l->count == 5);
    CHECK(cbj_num(cbj_at(l, 1), 0) == -25);
    CHECK(!strcmp(cbj_str(cbj_at(l, 2), ""), "a\"b\\c\n"));
    CHECK(cbj_at(l, 3)->type == CBJ_ARR && cbj_at(l, 3)->count == 0);
    CHECK(cbj_at(l, 9) == NULL);
    CHECK(cbj_get(r, "missing") == NULL);
    CHECK(!strcmp(cbj_str(NULL, "dflt"), "dflt"));
    cbj_free(r);
}

static void unicode(void)
{
    const char *t = "[\"caf\\u00e9 \\u2192 \\ud83d\\ude97\", \"\xC2\xB0\"]";
    cbj_t *r = cbj_parse(t, strlen(t));
    CHECK(r);
    CHECK(!strcmp(cbj_str(cbj_at(r, 0), ""), "caf\xC3\xA9 \xE2\x86\x92 \xF0\x9F\x9A\x97"));
    CHECK(!strcmp(cbj_str(cbj_at(r, 1), ""), "\xC2\xB0"));
    cbj_free(r);
}

static void malformed(void)
{
    const char *bad[] = { "", "{", "{\"a\":}", "[1,]", "{\"a\" 1}", "\"open", "tru", "[1] x", "{\"a\":1,}" };
    for(size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        cbj_t *r = cbj_parse(bad[i], strlen(bad[i]));
        if(r) printf("  accepted: %s\n", bad[i]);
        CHECK(r == NULL);
        cbj_free(r);
    }
}

int main(void)
{
    frame();
    unicode();
    malformed();
    if(fails) return 1;
    printf("ok\n");
    return 0;
}
