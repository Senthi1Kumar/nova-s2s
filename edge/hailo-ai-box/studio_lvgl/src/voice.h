#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Nova Voice client: WS /v1/voice/stream with the open-mic protocol of the voice addendum (sections 1 and 2).
   One worker thread owns the socket, the energy VAD, the turn state machine and reply-WAV parsing; PortAudio's
   callbacks only move samples through a lock-free ring (capture) and a try-locked buffer (playback), and never
   touch LVGL. The LVGL thread calls voice_take() for a snapshot. */
typedef enum { VOICE_IDLE, VOICE_HEARD, VOICE_THINKING, VOICE_SPEAKING } voice_state_t;

typedef struct {
    voice_state_t state;       /* IDLE = listening, waiting for speech */
    bool muted, connected, mic_ok, spk_ok, calibrated;
    uint32_t gen;              /* ++ whenever a text field or the timings change */
    uint32_t turn;             /* ++ per utterance sent */
    uint8_t level;             /* 0..255: mic RMS while HEARD, playback RMS while SPEAKING, else 0 (= synthesised) */
    char transcript[512], reply[2048], err[128], mic_name[64], spk_name[64];
    bool have_timings;
    double asr_ms, ttft_ms, llm_ms, tts_ms;
    double threshold;          /* VAD RMS threshold once calibrated */
} voice_view_t;

void voice_open(void);   /* connect, open the audio device, start listening (idempotent) */
void voice_close(void);  /* tear everything down */
void voice_set_model(const char *id);
void voice_set_muted(bool muted);
void voice_cancel(void);   /* Stop: cancel the turn, stop playback, back to listening */
void voice_take(voice_view_t *out);
