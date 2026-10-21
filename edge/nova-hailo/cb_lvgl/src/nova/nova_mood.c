#include "nova/nova_mood.h"

/* Signed window: valid for spans up to about 24 days. */
static bool due(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }

void nova_mood_init(nova_mood_t *m, uint32_t now)
{
    *m = (nova_mood_t){ .base = AV_CALM, .saved = AV_CALM, .last_active = now };
}

void nova_mood_event(nova_mood_t *m, nm_event_t ev, uint32_t now)
{
    /* Only speech or a reply counts as activity, but any event wakes a sleeper: restart its clock. */
    bool asleep = due(now, m->last_active + NM_SLEEPY_MS);
    if(asleep || ev == NM_SPEECH_STARTED || ev == NM_REPLY_DELTA) m->last_active = now;

    if(ev == NM_DUCK) {
        if(!m->ducked) m->saved = m->base;
        m->ducked = true;
        m->surprised = false;
        m->base = AV_CURIOUS;
        return;
    }
    if(ev == NM_UNDUCK) {
        if(m->ducked) m->base = m->saved;
        m->ducked = false;
        return;
    }
    if(ev == NM_INTERRUPTED) {
        m->ducked = false;
        m->turn_error = false;
        m->surprised = true;
        m->surprise_end = now + NM_SURPRISE_MS;
        m->base = AV_CALM;
        return;
    }

    /* The reply keeps streaming under a duck: update what unduck restores, keep showing curious. */
    if(m->ducked && (ev == NM_REPLY_DELTA || ev == NM_TOOL_ERROR)) {
        if(ev == NM_TOOL_ERROR) m->turn_error = true;
        m->saved = m->turn_error ? AV_CONCERNED : AV_HAPPY;
        return;
    }

    /* Every other event sets the mood itself, so it overrides a duck or a surprise. */
    m->ducked = false;
    m->surprised = false;
    switch(ev) {
    case NM_SESSION_START: m->turn_error = false; m->base = AV_HAPPY; break;
    case NM_SPEECH_STARTED: m->turn_error = false; m->base = AV_CURIOUS; break;
    case NM_TRANSCRIPT_DONE: m->base = AV_CALM; break;
    case NM_TOOL_ERROR: m->turn_error = true; m->base = AV_CONCERNED; break;
    case NM_REPLY_DELTA: m->base = m->turn_error ? AV_CONCERNED : AV_HAPPY; break;
    case NM_RESPONSE_DONE: m->turn_error = false; m->base = AV_CALM; break;
    default: break;
    }
}

av_mood_t nova_mood_get(nova_mood_t *m, uint32_t now)
{
    if(m->surprised) {
        if(!due(now, m->surprise_end)) return AV_SURPRISED;
        m->surprised = false;
    }
    if(due(now, m->last_active + NM_SLEEPY_MS)) return AV_SLEEPY;
    return m->base;
}
