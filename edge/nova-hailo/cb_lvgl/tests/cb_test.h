#pragma once
#include <stdio.h>

/* Minimal assert harness: every failing check prints file:line and is counted; DONE() sets the exit code. */
static int cb_failures;
#define CHECK(c) do { if(!(c)) { fprintf(stderr, "%s:%d CHECK(%s)\n", __FILE__, __LINE__, #c); cb_failures++; } } while(0)
#define CHECK_EQ(a, b) do { long long _a = (long long)(a), _b = (long long)(b); if(_a != _b) { \
    fprintf(stderr, "%s:%d %s == %lld, expected %lld\n", __FILE__, __LINE__, #a, _a, _b); cb_failures++; } } while(0)
#define RUN(t) t()
#define DONE() do { if(cb_failures) { fprintf(stderr, "%d failure(s)\n", cb_failures); return 1; } puts("ok"); return 0; } while(0)
