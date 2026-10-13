#include "cb_demo.h"

#define RESUME_AFTER_MS 1500u

static bool due(uint32_t now, uint32_t at) { return (int32_t)(now - at) >= 0; }

void cb_demo_init(cb_demo_t *d, const cb_step_t *steps, size_t n, cb_demo_io_t io, uint32_t now_ms,
                  uint32_t first_delay_ms)
{
    *d = (cb_demo_t){ .steps = steps, .n = n, .next_at = now_ms + first_delay_ms, .io = io };
}

/* Move `i` forward (wrapping) to the next step that starts a turn; stays put if `i` already is one. */
static void jump_to_turn_start(cb_demo_t *d)
{
    for(size_t k = 0; k < d->n; k++) {
        size_t j = (d->i + k) % d->n;
        if(d->steps[j].turn_start) {
            d->i = j;
            return;
        }
    }
}

void cb_demo_abort(cb_demo_t *d, uint32_t now_ms)
{
    d->run_id++;
    if(d->n) jump_to_turn_start(d);
    d->next_at = now_ms + RESUME_AFTER_MS;
}

void cb_demo_set_paused(cb_demo_t *d, bool paused) { d->paused = paused; }

void cb_demo_tick(cb_demo_t *d, uint32_t now_ms)
{
    /* At most one pass over the script per tick, so zero-delay steps cannot spin forever. */
    for(size_t guard = 0; guard < d->n && !d->paused && due(now_ms, d->next_at); guard++) {
        const cb_step_t *s = &d->steps[d->i];
        if(s->kind != DS_EVENT && !s->always && d->io.live && !d->io.live(d->io.ctx)) {
            /* Nobody is listening (muted, ended, or an unanswered follow-up): drop the rest of this turn. */
            d->i = (d->i + 1) % d->n;
            jump_to_turn_start(d);
            d->next_at = now_ms + RESUME_AFTER_MS;
            return;
        }
        d->i = (d->i + 1) % d->n;
        d->next_at = now_ms + s->after_ms;
        switch(s->kind) {
            case DS_EVENT: {
                unsigned fx = d->io.emit ? d->io.emit(d->io.ctx, s->ev, &s->done) : 0u;
                if(fx & NV_FX_ABORT) {
                    cb_demo_abort(d, now_ms);
                    return;
                }
                break;
            }
            case DS_USER:
                if(d->io.say) d->io.say(d->io.ctx, s->text, true);
                break;
            case DS_NOVA:
                if(d->io.say) d->io.say(d->io.ctx, s->text, false);
                break;
            case DS_APPLY:
                if(d->io.apply) d->io.apply(d->io.ctx, s->apply_id);
                break;
        }
    }
}
