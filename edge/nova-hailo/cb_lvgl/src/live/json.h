#pragma once
#include <stdbool.h>
#include <stddef.h>

/* A small JSON DOM for the observe stream: one allocation per document (nodes + unescaped strings), RFC 8259
   syntax, \u escapes decoded to UTF-8. Lookups are linear, which suits frames of a few hundred keys. */
typedef enum { CBJ_NULL, CBJ_BOOL, CBJ_NUM, CBJ_STR, CBJ_ARR, CBJ_OBJ } cbj_type_t;

typedef struct cbj {
    cbj_type_t type;
    const char *key;            /* member name inside an object, else NULL */
    bool b;
    double n;
    const char *s;
    struct cbj *child, *next;   /* first child / next sibling */
    int count;                  /* children of an array or object */
} cbj_t;

cbj_t *cbj_parse(const char *text, size_t len);   /* NULL when malformed */
void cbj_free(cbj_t *root);
const cbj_t *cbj_get(const cbj_t *obj, const char *key);
const cbj_t *cbj_at(const cbj_t *arr, int i);
const char *cbj_str(const cbj_t *n, const char *def);   /* strings only */
double cbj_num(const cbj_t *n, double def);             /* numbers only */
bool cbj_bool(const cbj_t *n, bool def);                /* booleans only */
