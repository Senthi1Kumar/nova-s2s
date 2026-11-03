#include "../src/voice_pcm.h"
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TEST_PI 3.14159265358979323846
static int fails;
#define CHECK(c) do { if(!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); fails++; } } while(0)

static pcm_ring_t ring;

static size_t drain(int16_t *out, size_t cap)
{
    return pcm_ring_read(&ring, out, cap);
}

/* sine at 16 kHz as PCM16LE bytes */
static uint8_t *sine_bytes(size_t n)
{
    uint8_t *b = malloc(n * 2);
    for(size_t i = 0; i < n; i++) {
        int16_t v = (int16_t)(12000.0 * sin(2 * TEST_PI * 440.0 * (double)i / 16000.0));
        b[2 * i] = (uint8_t)(v & 0xff);
        b[2 * i + 1] = (uint8_t)((uint16_t)v >> 8);
    }
    return b;
}

static void resample_case(int out_rate, const size_t *chunks, size_t nchunks)
{
    const size_t n = 16000;
    uint8_t *b = sine_bytes(n);
    rs_t rs;
    rs_init(&rs, 16000, out_rate);
    pcm_ring_init(&ring);
    static int16_t out[PB_N / 8];
    size_t pos = 0, got = 0, k = 0;
    while(pos < n * 2) {
        size_t c = chunks[k++ % nchunks];
        if(pos + c > n * 2) c = n * 2 - pos;
        rs_push(&rs, b + pos, c, &ring);
        got += drain(out + got, sizeof out / sizeof *out - got);
        pos += c;
    }
    long expect = (long)((double)n * out_rate / 16000.0);
    CHECK(labs((long)got - expect) <= 1);
    /* a 440 Hz sine at 12000 amplitude moves at most ~2*pi*440/out_rate*12000 per sample */
    double maxstep = 2 * TEST_PI * 440.0 / out_rate * 12000.0 * 1.2 + 4;
    long worst = 0;
    for(size_t i = 1; i < got; i++) {
        long d = labs((long)out[i] - out[i - 1]);
        if(d > worst) worst = d;
    }
    CHECK((double)worst <= maxstep);
    free(b);
}

static void test_resample(void)
{
    const size_t odd[] = { 1, 3, 7, 2, 5, 11, 1, 63 };
    const size_t even[] = { 2, 640, 6, 1280, 20, 64 };
    resample_case(48000, odd, 8);
    resample_case(48000, even, 6);
    resample_case(44100, odd, 8);
    resample_case(44100, even, 6);
    resample_case(16000, odd, 8);
}

static void test_odd_carry(void)
{
    int16_t samples[4] = { 0x0102, -2, 0x7fff, -32768 };
    uint8_t b[8];
    for(int i = 0; i < 4; i++) { b[2 * i] = (uint8_t)(samples[i] & 0xff); b[2 * i + 1] = (uint8_t)((uint16_t)samples[i] >> 8); }
    rs_t rs;
    rs_init(&rs, 16000, 16000);
    pcm_ring_init(&ring);
    CHECK(rs_push(&rs, b, 3, &ring) == 1);   /* second sample split: one byte carried */
    CHECK(rs.has_carry);
    CHECK(rs_push(&rs, b + 3, 5, &ring) == 3);
    CHECK(!rs.has_carry);
    int16_t out[8];
    CHECK(drain(out, 8) == 4);
    CHECK(memcmp(out, samples, sizeof samples) == 0);
    /* single byte pushes */
    pcm_ring_init(&ring);
    rs_reset(&rs);
    for(int i = 0; i < 8; i++) rs_push(&rs, b + i, 1, &ring);
    CHECK(drain(out, 8) == 4);
    CHECK(memcmp(out, samples, sizeof samples) == 0);
    /* a carried byte is dropped by reset */
    rs_push(&rs, b, 1, &ring);
    rs_reset(&rs);
    CHECK(!rs.has_carry);
}

static void test_ring(void)
{
    static int16_t in[1000], out[1000];
    for(int i = 0; i < 1000; i++) in[i] = (int16_t)i;
    pcm_ring_init(&ring);
    /* wrap-around: push the positions past PB_N */
    for(unsigned round = 0; round < (PB_N / 1000) + 3; round++) {
        CHECK(pcm_ring_write(&ring, in, 1000) == 1000);
        CHECK(pcm_ring_avail(&ring) == 1000);
        CHECK(pcm_ring_read(&ring, out, 1000) == 1000);
        CHECK(memcmp(in, out, sizeof in) == 0);
    }
    /* overflow drops the tail */
    pcm_ring_init(&ring);
    size_t total = 0;
    for(unsigned i = 0; i < PB_N / 1000 + 2; i++) total += pcm_ring_write(&ring, in, 1000);
    CHECK(total == PB_N);
    CHECK(pcm_ring_avail(&ring) == PB_N);
    CHECK(pcm_ring_write(&ring, in, 10) == 0);
    CHECK(pcm_ring_read(&ring, out, 4) == 4 && out[3] == 3);
    /* flush: avail reads 0, the consumer drops what was written, later writes survive */
    pcm_ring_init(&ring);
    pcm_ring_write(&ring, in, 500);
    pcm_ring_request_flush(&ring);
    CHECK(pcm_ring_avail(&ring) == 0);
    pcm_ring_write(&ring, in + 500, 100);
    CHECK(pcm_ring_read(&ring, out, 1000) == 100);
    CHECK(out[0] == 500 && out[99] == 599);
    CHECK(pcm_ring_avail(&ring) == 0);
    /* flush on an empty ring is harmless */
    pcm_ring_request_flush(&ring);
    CHECK(pcm_ring_read(&ring, out, 10) == 0);
    pcm_ring_write(&ring, in, 5);
    CHECK(pcm_ring_avail(&ring) == 5);
}

static void test_sent(void)
{
    sent_t s;
    sent_reset(&s);
    CHECK(!sent_open(&s));
    sent_start(&s, 0, 2560);
    CHECK(sent_open(&s));
    sent_bytes(&s, 1280);
    sent_bytes(&s, 1280);
    CHECK(sent_end(&s, 0, 2560));
    CHECK(!sent_open(&s));
    sent_start(&s, 1, 1280);
    sent_bytes(&s, 640);
    CHECK(!sent_end(&s, 1, 1280));   /* short */
    CHECK(s.starts == 2 && s.ends == 2);
    CHECK(s.tot_expect == 3840 && s.tot_got == 3200);
    sent_start(&s, 2, 1280);
    sent_bytes(&s, 1280);
    CHECK(!sent_end(&s, 3, 1280));   /* wrong sequence */
    /* native streaming: the total is unknown until sentence end */
    sent_reset(&s);
    sent_start(&s, 0, 0);
    sent_bytes(&s, 1280);
    sent_bytes(&s, 2560);
    CHECK(sent_end(&s, 0, 3840));
    CHECK(s.tot_got == 3840 && s.tot_expect == 3840);
}

static void test_finished(void)
{
    sent_t s;
    sent_reset(&s);
    CHECK(turn_finished(true, &s, true));      /* empty reply */
    CHECK(!turn_finished(false, &s, true));
    CHECK(!turn_finished(true, &s, false));
    sent_start(&s, 0, 1280);
    CHECK(!turn_finished(true, &s, true));     /* sentence still open */
    sent_bytes(&s, 1280);
    sent_end(&s, 0, 1280);
    CHECK(turn_finished(true, &s, true));
    CHECK(!turn_finished(true, &s, false));
    CHECK(!turn_finished(false, &s, false));
}

/* One speaker-callback pass, the same decisions as spk_cb in voice.c. */
static size_t cb_step(bool *primed, bool sent_is_open, size_t prebuf, int16_t *out, size_t frames)
{
    if(pcm_ring_apply_flush(&ring)) *primed = false;
    if(!*primed && pcm_should_prime(pcm_ring_avail(&ring), prebuf, sent_is_open)) *primed = true;
    size_t n = 0;
    if(*primed) {
        n = pcm_ring_read(&ring, out, frames);
        if(n < frames) *primed = false;
    }
    return n;
}

static void test_callback_flush(void)
{
    static int16_t old[400], fresh[400], out[400];
    for(int i = 0; i < 400; i++) { old[i] = (int16_t)(1000 + i); fresh[i] = (int16_t)(5000 + i); }
    bool primed = false;
    pcm_ring_init(&ring);
    pcm_ring_write(&ring, old, 400);
    CHECK(cb_step(&primed, true, 300, out, 100) == 100 && out[0] == 1000);
    pcm_ring_request_flush(&ring);
    CHECK(cb_step(&primed, false, 300, out, 100) == 0);   /* flush applied, nothing left to play */
    CHECK(!primed);
    pcm_ring_write(&ring, fresh, 400);
    CHECK(cb_step(&primed, true, 300, out, 100) == 100);
    CHECK(out[0] == 5000 && out[99] == 5099);
    /* a flush requested while unprimed (the deadlock case) is still applied */
    primed = false;
    pcm_ring_init(&ring);
    pcm_ring_write(&ring, old, 100);
    pcm_ring_request_flush(&ring);
    CHECK(cb_step(&primed, true, 300, out, 50) == 0);
    pcm_ring_write(&ring, fresh, 400);
    CHECK(cb_step(&primed, true, 300, out, 50) == 50 && out[0] == 5000);
    CHECK(pcm_should_prime(10, 300, false) && !pcm_should_prime(10, 300, true) && !pcm_should_prime(0, 300, false));
}

static void test_chunk_invariance(void)
{
    const size_t n = 4000;
    uint8_t *b = sine_bytes(n);
    static int16_t whole[PB_N / 16], parts[PB_N / 16];
    size_t nw, np = 0;
    rs_t rs;
    rs_init(&rs, 16000, 44100);
    pcm_ring_init(&ring);
    rs_push(&rs, b, n * 2, &ring);
    nw = drain(whole, sizeof whole / sizeof *whole);
    rs_init(&rs, 16000, 44100);
    pcm_ring_init(&ring);
    const size_t chunks[] = { 1, 5, 3, 640, 7, 2 };
    size_t pos = 0, k = 0;
    while(pos < n * 2) {
        size_t c = chunks[k++ % 6];
        if(pos + c > n * 2) c = n * 2 - pos;
        rs_push(&rs, b + pos, c, &ring);
        np += drain(parts + np, sizeof parts / sizeof *parts - np);
        pos += c;
    }
    CHECK(nw == np);
    CHECK(nw && memcmp(whole, parts, nw * sizeof *whole) == 0);
    free(b);
}

int main(void)
{
    test_resample();
    test_odd_carry();
    test_ring();
    test_sent();
    test_finished();
    test_callback_flush();
    test_chunk_invariance();
    if(fails) { printf("%d check(s) failed\n", fails); return 1; }
    printf("voice_pcm: ok\n");
    return 0;
}
