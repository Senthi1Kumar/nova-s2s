#pragma once
#include "nova/avatar_model.h"
#include <stdbool.h>
#include <stdint.h>

/* Nova's realtime events -> the avatar's mood. Pure C, no LVGL: feed it events as they arrive, read the
   mood each frame. Times are wrapping uint32 ms. */
typedef enum { NM_SESSION_START, NM_SPEECH_STARTED, NM_TRANSCRIPT_DONE, NM_TOOL_ERROR, NM_REPLY_DELTA,
               NM_DUCK, NM_UNDUCK, NM_INTERRUPTED, NM_RESPONSE_DONE } nm_event_t;

#define NM_SURPRISE_MS 1000u
#define NM_SLEEPY_MS (5u * 60u * 1000u)

typedef struct {
    av_mood_t base, saved;            /* saved: the mood to restore on unduck */
    bool ducked, turn_error, surprised;
    uint32_t surprise_end, last_active;
} nova_mood_t;

void nova_mood_init(nova_mood_t *m, uint32_t now);
void nova_mood_event(nova_mood_t *m, nm_event_t ev, uint32_t now);
av_mood_t nova_mood_get(nova_mood_t *m, uint32_t now);
