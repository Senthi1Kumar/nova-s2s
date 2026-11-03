#pragma once
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Streaming reply audio for the Studio voice client: a lock-free SPSC sample ring (worker thread writes, the
   PortAudio speaker callback reads), a streaming linear resampler for the server's raw PCM16 chunks, and the
   per-sentence byte accounting that decides when a turn is finished. Pure C11: no PortAudio, no LVGL. */

#define PB_N (1u << 21)   /* samples: about 43 s at 48 kHz, power of two */

typedef struct {
    int16_t buf[PB_N];
    atomic_uint rpos, wpos;         /* free-running; only the consumer moves rpos */
    atomic_uint flush_pos;          /* producer's wpos when the flush was requested */
    atomic_bool flush_req;
} pcm_ring_t;

void pcm_ring_init(pcm_ring_t *r);
size_t pcm_ring_write(pcm_ring_t *r, const int16_t *s, size_t n);   /* producer; drops what does not fit */
size_t pcm_ring_read(pcm_ring_t *r, int16_t *out, size_t n);        /* consumer; applies a pending flush first */
bool pcm_ring_apply_flush(pcm_ring_t *r);                          /* consumer side, every callback: true if a flush was applied */
bool pcm_should_prime(size_t avail, size_t prebuffer, bool sent_open);   /* start playing: prebuffer queued, or the last sentence ended and something is left */
size_t pcm_ring_avail(const pcm_ring_t *r);                         /* 0 while a flush is pending */
void pcm_ring_request_flush(pcm_ring_t *r);                         /* producer side; the consumer drops everything written so far */

typedef struct {
    double phase, step;   /* step = input samples per output sample */
    int16_t prev;
    uint8_t carry;        /* low byte of a sample split across two pushes */
    bool has_carry;
} rs_t;

void rs_init(rs_t *rs, int in_rate, int out_rate);
void rs_reset(rs_t *rs);
/* PCM16LE bytes of any length in, resampled samples out to `ring`. Returns samples written. */
size_t rs_push(rs_t *rs, const uint8_t *bytes, size_t len, pcm_ring_t *ring);

typedef struct {
    int open_seq;                 /* -1 = no sentence open */
    uint32_t expect, got;         /* current sentence */
    int starts, ends;
    uint32_t tot_expect, tot_got; /* whole turn, for the log line */
} sent_t;

void sent_reset(sent_t *s);
void sent_start(sent_t *s, int seq, uint32_t bytes);
void sent_bytes(sent_t *s, size_t n);
bool sent_end(sent_t *s, int seq, uint32_t bytes);   /* false when the byte counts or the sequence disagree */
bool sent_open(const sent_t *s);
bool turn_finished(bool done_seen, const sent_t *s, bool ring_empty);
