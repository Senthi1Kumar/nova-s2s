#include "voice_pcm.h"

void pcm_ring_init(pcm_ring_t *r)
{
    atomic_store(&r->rpos, 0);
    atomic_store(&r->wpos, 0);
    atomic_store(&r->flush_pos, 0);
    atomic_store(&r->flush_req, false);
}

size_t pcm_ring_write(pcm_ring_t *r, const int16_t *s, size_t n)
{
    unsigned w = atomic_load_explicit(&r->wpos, memory_order_relaxed);
    unsigned rd = atomic_load_explicit(&r->rpos, memory_order_acquire);
    size_t room = PB_N - (size_t)(w - rd);
    if(n > room) n = room;   /* full: drop the rest */
    for(size_t i = 0; i < n; i++) r->buf[(w + (unsigned)i) & (PB_N - 1)] = s[i];
    atomic_store_explicit(&r->wpos, w + (unsigned)n, memory_order_release);
    return n;
}

bool pcm_ring_apply_flush(pcm_ring_t *r)
{
    if(!atomic_exchange_explicit(&r->flush_req, false, memory_order_acquire)) return false;
    unsigned rd = atomic_load_explicit(&r->rpos, memory_order_relaxed);
    unsigned to = atomic_load_explicit(&r->flush_pos, memory_order_relaxed);
    if((int)(to - rd) > 0) atomic_store_explicit(&r->rpos, to, memory_order_release);
    return true;
}

bool pcm_should_prime(size_t avail, size_t prebuffer, bool sent_open)
{
    return avail >= prebuffer || (avail && !sent_open);
}

size_t pcm_ring_read(pcm_ring_t *r, int16_t *out, size_t n)
{
    pcm_ring_apply_flush(r);
    unsigned rd = atomic_load_explicit(&r->rpos, memory_order_relaxed);
    unsigned w = atomic_load_explicit(&r->wpos, memory_order_acquire);
    size_t have = (size_t)(w - rd);
    if(n > have) n = have;
    for(size_t i = 0; i < n; i++) out[i] = r->buf[(rd + (unsigned)i) & (PB_N - 1)];
    atomic_store_explicit(&r->rpos, rd + (unsigned)n, memory_order_release);
    return n;
}

size_t pcm_ring_avail(const pcm_ring_t *r)
{
    if(atomic_load_explicit(&r->flush_req, memory_order_acquire)) return 0;
    return (size_t)(atomic_load_explicit(&r->wpos, memory_order_acquire) - atomic_load_explicit(&r->rpos, memory_order_acquire));
}

void pcm_ring_request_flush(pcm_ring_t *r)
{
    atomic_store_explicit(&r->flush_pos, atomic_load_explicit(&r->wpos, memory_order_relaxed), memory_order_relaxed);
    atomic_store_explicit(&r->flush_req, true, memory_order_release);
}

void rs_init(rs_t *rs, int in_rate, int out_rate)
{
    rs->step = out_rate > 0 ? (double)in_rate / out_rate : 1.0;
    rs_reset(rs);
}

void rs_reset(rs_t *rs)
{
    rs->phase = 0;
    rs->prev = 0;
    rs->carry = 0;
    rs->has_carry = false;
}

#define RS_BATCH 256

/* Same interpolation as the capture callback: out = prev + (v - prev) * phase. */
size_t rs_push(rs_t *rs, const uint8_t *bytes, size_t len, pcm_ring_t *ring)
{
    int16_t out[RS_BATCH];
    size_t n = 0, total = 0, i = 0;
    bool pass = rs->step == 1.0;
    while(i < len) {
        int16_t v;
        if(rs->has_carry) {
            v = (int16_t)(uint16_t)(rs->carry | bytes[i] << 8);
            rs->has_carry = false;
            i++;
        }
        else if(i + 1 < len) {
            v = (int16_t)(uint16_t)(bytes[i] | bytes[i + 1] << 8);
            i += 2;
        }
        else {
            rs->carry = bytes[i];
            rs->has_carry = true;
            i++;
            break;
        }
        if(pass) {
            out[n++] = v;
            if(n == RS_BATCH) { total += pcm_ring_write(ring, out, n); n = 0; }
            continue;
        }
        while(rs->phase < 1.0) {
            out[n++] = (int16_t)(rs->prev + ((double)v - rs->prev) * rs->phase);
            rs->phase += rs->step;
            if(n == RS_BATCH) { total += pcm_ring_write(ring, out, n); n = 0; }
        }
        rs->phase -= 1.0;
        rs->prev = v;
    }
    if(n) total += pcm_ring_write(ring, out, n);
    return total;
}

void sent_reset(sent_t *s)
{
    s->open_seq = -1;
    s->expect = s->got = 0;
    s->starts = s->ends = 0;
    s->tot_expect = s->tot_got = 0;
}

void sent_start(sent_t *s, int seq, uint32_t bytes)
{
    s->open_seq = seq;
    s->expect = bytes;
    s->got = 0;
    s->starts++;
    s->tot_expect += bytes;
}

void sent_bytes(sent_t *s, size_t n)
{
    s->got += (uint32_t)n;
    s->tot_got += (uint32_t)n;
}

bool sent_end(sent_t *s, int seq, uint32_t bytes)
{
    bool ok = s->open_seq == seq && s->got == bytes && (!s->expect || s->expect == bytes);
    if(!s->expect) s->tot_expect += bytes;
    s->open_seq = -1;
    s->ends++;
    return ok;
}

bool sent_open(const sent_t *s) { return s->open_seq >= 0; }

bool turn_finished(bool done_seen, const sent_t *s, bool ring_empty)
{
    return done_seen && s->starts == s->ends && ring_empty;
}
