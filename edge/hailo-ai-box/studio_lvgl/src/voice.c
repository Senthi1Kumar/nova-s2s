#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "voice.h"
#include "json.h"
#include "net.h"
#include "voice_pcm.h"
#include "ws.h"
#include <math.h>
#include <portaudio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define RATE 16000
#define FRAME 320                         /* 20 ms */
#define RING_N 65536                      /* mic ring, samples (4 s), power of two */
#define PRE_FRAMES 23                     /* 150 ms detection + 300 ms pre-roll */
#define START_FRAMES 8                    /* 160 ms above the threshold starts an utterance (>= 150 ms) */
#define END_FRAMES 35                     /* 700 ms below it ends one */
#define MAX_UTT_FRAMES (30 * 50)
#define BARGE_FRAMES 15                   /* 300 ms above 2x the threshold while the reply plays asks to interrupt it */
#define BARGE_THR_MULT 2.0
#define BARGE_WAIT_MS 1500                /* no `interrupted` this long after `stop` = the reply plays on */
#define CALIB_FRAMES 50                   /* first second of room noise */
#define MIN_THRESHOLD 350.0
#define MIC_GAIN 3                        /* XVF3800 channel 1 */
#define PREBUFFER_MS 800                  /* cover Phonon's bursty native chunk production */
#define SPEAKER_TAIL_MS 250               /* ring empty this long = the device buffer has played out */
#define SPEAKER_HANG_MS 2000              /* slack beyond the queued audio's duration before SPEAKING is abandoned */
#define STALL_MS 15000                    /* no reply bytes this long mid-turn = give up */

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static struct {
    pthread_t th;
    bool running, joined;
    atomic_bool stop;

    /* commands from the LVGL thread */
    pthread_mutex_t cmd_mu;
    char model[64];
    atomic_bool muted, cancel;

    /* view shared with the LVGL thread */
    pthread_mutex_t view_mu;
    voice_view_t view;
    atomic_int level;

    /* capture: SPSC ring, written by the PortAudio callback */
    PaStream *in, *out;
    int in_ch, in_idx, out_ch;
    double in_rate, out_rate, mic_gain;
    int16_t ring[RING_N];
    atomic_uint rpos, wpos;
    double cap_phase, cap_prev;

    /* playback: SPSC ring at out_rate, written by the worker (on_bin), read by the speaker callback */
    pcm_ring_t pb;
    atomic_bool primed;        /* callback-owned: prebuffer reached, audio is playing */
    atomic_bool sent_open;     /* worker-owned: a sentence is still streaming in */
} V = { .cmd_mu = PTHREAD_MUTEX_INITIALIZER, .view_mu = PTHREAD_MUTEX_INITIALIZER };

/* ---------- view helpers ---------- */

#define VIEW(...) do { pthread_mutex_lock(&V.view_mu); __VA_ARGS__; pthread_mutex_unlock(&V.view_mu); } while(0)

static void set_state(voice_state_t s) { VIEW(V.view.state = s); }

static void set_err(const char *e)
{
    VIEW(snprintf(V.view.err, sizeof V.view.err, "%s", e); V.view.gen++);
}

void voice_take(voice_view_t *out)
{
    pthread_mutex_lock(&V.view_mu);
    *out = V.view;
    pthread_mutex_unlock(&V.view_mu);
    out->muted = atomic_load(&V.muted);
    out->level = (uint8_t)atomic_load(&V.level);
}

void voice_set_model(const char *id)
{
    pthread_mutex_lock(&V.cmd_mu);
    snprintf(V.model, sizeof V.model, "%s", id && *id ? id : "qwen3-vl");
    pthread_mutex_unlock(&V.cmd_mu);
}

void voice_set_muted(bool m)
{
    atomic_store(&V.muted, m);
    VIEW(V.view.muted = m);
}

void voice_cancel(void) { atomic_store(&V.cancel, true); }

/* ---------- audio ---------- */

static bool name_has(const char *n, const char *s) { return n && strstr(n, s); }

static int find_device(bool input)
{
    const char *prefer = getenv(input ? "NOVA_CAPTURE_DEVICE" : "NOVA_PLAYBACK_DEVICE");
    int n = Pa_GetDeviceCount(), array = paNoDevice;
    for(int i = 0; i < n; i++) {
        const PaDeviceInfo *d = Pa_GetDeviceInfo(i);
        if(!d || (input ? d->maxInputChannels : d->maxOutputChannels) < 1) continue;
        if(prefer && name_has(d->name, prefer)) return i;
        if(name_has(d->name, "XVF3800") || name_has(d->name, "reSpeaker")) array = i;
    }
    if(array != paNoDevice) return array;
    return input ? Pa_GetDefaultInputDevice() : Pa_GetDefaultOutputDevice();
}

static inline void ring_push(int16_t s)
{
    unsigned w = atomic_load_explicit(&V.wpos, memory_order_relaxed);
    unsigned r = atomic_load_explicit(&V.rpos, memory_order_acquire);
    if(w - r >= RING_N) return;   /* full: drop */
    V.ring[w & (RING_N - 1)] = s;
    atomic_store_explicit(&V.wpos, w + 1, memory_order_release);
}

/* Never blocks, never allocates, never touches LVGL. Channel `in_idx` x gain 3 (clipped), resampled to 16 kHz. */
static int mic_cb(const void *input, void *output, unsigned long frames, const PaStreamCallbackTimeInfo *ti, PaStreamCallbackFlags fl, void *ud)
{
    (void)output; (void)ti; (void)fl; (void)ud;
    if(!input) return paContinue;
    const int16_t *in = input;
    double step = V.in_rate / RATE;
    for(unsigned long i = 0; i < frames; i++) {
        int v = in[i * (unsigned long)V.in_ch + (unsigned long)V.in_idx];
        v = (int)(v * V.mic_gain);
        if(v > 32767) v = 32767;
        if(v < -32768) v = -32768;
        while(V.cap_phase < 1.0) {
            ring_push((int16_t)(V.cap_prev + ((double)v - V.cap_prev) * V.cap_phase));
            V.cap_phase += step;
        }
        V.cap_phase -= 1.0;
        V.cap_prev = v;
    }
    return paContinue;
}

/* Never blocks or allocates. Silence until PREBUFFER_MS is queued (or the last sentence has ended), then the ring;
   an underrun plays silence and re-buffers, the turn stays in SPEAKING. */
static int spk_cb(const void *input, void *output, unsigned long frames, const PaStreamCallbackTimeInfo *ti, PaStreamCallbackFlags fl, void *ud)
{
    (void)input; (void)ti; (void)fl; (void)ud;
    int16_t *o = output;
    int ch = V.out_ch;
    memset(o, 0, frames * (unsigned long)ch * sizeof *o);
    if(pcm_ring_apply_flush(&V.pb)) atomic_store(&V.primed, false);
    if(!atomic_load(&V.primed) && pcm_should_prime(pcm_ring_avail(&V.pb), (size_t)(V.out_rate * PREBUFFER_MS / 1000.0), atomic_load(&V.sent_open)))
        atomic_store(&V.primed, true);
    unsigned long n = 0;
    double acc = 0;
    if(atomic_load(&V.primed)) {
        int16_t tmp[512];
        while(n < frames) {
            unsigned long want = frames - n < 512 ? frames - n : 512;
            size_t got = pcm_ring_read(&V.pb, tmp, want);
            if(!got) break;
            for(size_t i = 0; i < got; i++) {
                acc += (double)tmp[i] * tmp[i];
                for(int c = 0; c < ch; c++) o[(n + i) * (unsigned long)ch + (unsigned long)c] = tmp[i];
            }
            n += got;
        }
        if(n < frames) atomic_store(&V.primed, false);
    }
    double rms = n ? sqrt(acc / (double)n) : 0;
    int lv = (int)(rms / 5000.0 * 255.0);
    atomic_store(&V.level, lv > 255 ? 255 : lv);
    return paContinue;
}

static void audio_open(void)
{
    PaError e = Pa_Initialize();
    if(e != paNoError) {
        set_err("audio: PortAudio init failed");
        return;
    }
    int din = find_device(true);
    const PaDeviceInfo *ii = din >= 0 ? Pa_GetDeviceInfo(din) : NULL;
    if(ii) {
        bool xvf = name_has(ii->name, "XVF3800") || name_has(ii->name, "reSpeaker");
        V.in_ch = xvf && ii->maxInputChannels >= 2 ? 2 : 1;
        V.in_idx = xvf && V.in_ch > 1 ? 1 : 0;
        V.mic_gain = xvf && V.in_ch > 1 ? MIC_GAIN : 1.0;
        const char *ec = getenv("NOVA_CAPTURE_CHANNEL"), *eg = getenv("NOVA_MIC_GAIN");
        if(ec && *ec) {
            V.in_idx = atoi(ec) > 0 ? 1 : 0;
            if(V.in_idx >= ii->maxInputChannels) V.in_idx = 0;
            V.in_ch = V.in_idx + 1;
        }
        if(eg && *eg && atof(eg) > 0) V.mic_gain = atof(eg);
        PaStreamParameters p = { .device = din, .channelCount = V.in_ch, .sampleFormat = paInt16, .suggestedLatency = ii->defaultLowInputLatency };
        double rates[2] = { RATE, ii->defaultSampleRate };
        for(int k = 0; k < 2 && !V.in; k++) {
            V.in_rate = rates[k];
            V.cap_phase = 0;
            V.cap_prev = 0;
            if(Pa_OpenStream(&V.in, &p, NULL, V.in_rate, RATE / 100, paNoFlag, mic_cb, NULL) != paNoError) V.in = NULL;
        }
        if(V.in && Pa_StartStream(V.in) != paNoError) {
            Pa_CloseStream(V.in);
            V.in = NULL;
        }
    }
    int dout = find_device(false);
    const PaDeviceInfo *oi = dout >= 0 ? Pa_GetDeviceInfo(dout) : NULL;
    if(oi) {
        for(int ch = 1; ch <= 2 && !V.out; ch++) {
            PaStreamParameters p = { .device = dout, .channelCount = ch, .sampleFormat = paInt16, .suggestedLatency = oi->defaultLowOutputLatency };
            V.out_rate = oi->defaultSampleRate > 0 ? oi->defaultSampleRate : RATE;
            V.out_ch = ch;
            if(Pa_OpenStream(&V.out, NULL, &p, V.out_rate, 0, paNoFlag, spk_cb, NULL) != paNoError) V.out = NULL;
        }
        if(V.out && Pa_StartStream(V.out) != paNoError) {
            Pa_CloseStream(V.out);
            V.out = NULL;
        }
    }
    VIEW(V.view.mic_ok = V.in != NULL; V.view.spk_ok = V.out != NULL;
         snprintf(V.view.mic_name, sizeof V.view.mic_name, "%s", V.in && ii ? ii->name : "");
         snprintf(V.view.spk_name, sizeof V.view.spk_name, "%s", V.out && oi ? oi->name : ""));
    const char *processing = ii && (name_has(ii->name, "XVF3800") || name_has(ii->name, "reSpeaker"))
        ? "onboard AEC" : ii && name_has(ii->name, "pulse") ? "PipeWire playback-reference AEC" : "platform/default";
    printf("voice: mic %s (%s, ch%d@%.0f, gain %.1f, %s)  speaker %s (@%.0f x%d)\n",
           V.in ? ii->name : "none", V.in ? "open" : "-", V.in_idx, V.in_rate, V.mic_gain, processing,
           V.out ? oi->name : "none", V.out_rate, V.out_ch);
    fflush(stdout);
}

static void audio_close(void)
{
    if(V.in) { Pa_StopStream(V.in); Pa_CloseStream(V.in); V.in = NULL; }
    if(V.out) { Pa_StopStream(V.out); Pa_CloseStream(V.out); V.out = NULL; }
    Pa_Terminate();
}

/* Worker thread only: the speaker callback drops whatever is queued on its next pass. */
static void playback_stop(void)
{
    pcm_ring_request_flush(&V.pb);
    atomic_store(&V.primed, false);
    atomic_store(&V.sent_open, false);
    atomic_store(&V.level, 0);
}

/* ---------- WAV ---------- */

static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

/* RIFF PCM16 -> mono int16 at `to_rate` (linear). Returns a malloc'd buffer or NULL. */
static int16_t *wav_decode(const uint8_t *w, size_t len, double to_rate, size_t *out_n, int *src_rate)
{
    if(len < 44 || memcmp(w, "RIFF", 4) || memcmp(w + 8, "WAVE", 4)) return NULL;
    int ch = 0, bits = 0, fmt = 0, rate = 0;
    size_t off = 12, dpos = 0, dlen = 0;
    while(off + 8 <= len) {
        size_t sz = le32(w + off + 4);
        if(!memcmp(w + off, "fmt ", 4) && off + 8 + 16 <= len) {
            fmt = le16(w + off + 8);
            ch = le16(w + off + 10);
            rate = (int)le32(w + off + 12);
            bits = le16(w + off + 22);
        }
        else if(!memcmp(w + off, "data", 4)) {
            dpos = off + 8;
            dlen = sz;
            if(dpos + dlen > len) dlen = len - dpos;
            break;
        }
        off += 8 + sz + (sz & 1);
    }
    if(fmt != 1 || bits != 16 || ch < 1 || ch > 2 || rate < 4000 || !dlen) return NULL;
    size_t n = dlen / (size_t)(2 * ch);
    const uint8_t *d = w + dpos;
    int16_t *mono = malloc((n + 1) * sizeof *mono);
    if(!mono) return NULL;
    for(size_t i = 0; i < n; i++) {
        int s = (int16_t)le16(d + i * (size_t)(2 * ch));
        if(ch == 2) s = (s + (int16_t)le16(d + i * 4 + 2)) / 2;
        mono[i] = (int16_t)s;
    }
    if(src_rate) *src_rate = rate;
    if(fabs(rate - to_rate) < 1.0) {
        *out_n = n;
        return mono;
    }
    size_t m = (size_t)((double)n * to_rate / rate);
    int16_t *o = malloc((m + 1) * sizeof *o);
    if(!o) { free(mono); return NULL; }
    double step = rate / to_rate;
    for(size_t i = 0; i < m; i++) {
        double p = (double)i * step;
        size_t k = (size_t)p;
        double f = p - (double)k;
        int a = mono[k < n ? k : n - 1], b = mono[k + 1 < n ? k + 1 : n - 1];
        o[i] = (int16_t)(a + (b - a) * f);
    }
    free(mono);
    *out_n = m;
    return o;
}

/* ---------- worker ---------- */

typedef struct {
    ws_client_t *ws;
    bool open, discard, done_seen, cutoff_pending;
    bool barge_in, barging;                          /* STUDIO_BARGE_IN=1; a barge-in utterance is streaming */
    uint32_t barge_wait;                             /* until when an `interrupted` is still expected after `stop`; 0 = none */
    int turn_id;
    voice_state_t st;
    uint32_t t_open, t_stop, t_done, t_hold;
    /* streamed reply audio */
    rs_t rs;
    sent_t sent;
    uint32_t t_speech, t_first_pcm, t_last, t_fin;   /* t_first_pcm, t_fin: 0 = not yet */
    size_t done_queued;                              /* samples still queued when `done` arrived */
    /* VAD */
    int16_t pre[PRE_FRAMES][FRAME];
    int pre_n, pre_head, above, below, utt_frames, calib_n;
    double calib_sum, thr;
    /* test injection */
    int16_t *inj;
    size_t inj_n, inj_pos;
    bool injecting, injected;
} W;

static void on_open(void *ud) { ((W *)ud)->open = true; }

static void turn_reset(W *w)
{
    rs_reset(&w->rs);
    sent_reset(&w->sent);
    atomic_store(&V.sent_open, false);
    w->done_seen = false;
    w->done_queued = 0;
    w->t_first_pcm = w->t_fin = 0;
}

static void go_idle(W *w, uint32_t holdoff_ms)
{
    w->st = VOICE_IDLE;
    w->t_hold = now_ms() + holdoff_ms;
    w->above = w->below = w->pre_n = 0;
    w->barging = false;
    w->barge_wait = 0;
    playback_stop();
    turn_reset(w);
    set_state(VOICE_IDLE);
    atomic_store(&V.level, 0);
    atomic_store(&V.rpos, atomic_load(&V.wpos));   /* drop audio captured while gated */
}

static void on_bin(void *ud, const uint8_t *data, size_t len)
{
    W *w = ud;
    if(w->discard || (w->st != VOICE_THINKING && w->st != VOICE_SPEAKING) || !sent_open(&w->sent)) return;
    uint32_t now = now_ms();
    w->t_last = now;
    sent_bytes(&w->sent, len);
    if(!w->t_first_pcm) {
        w->t_first_pcm = now;
        if(!V.out) set_err("no speaker: reply shown as text");
    }
    if(w->st == VOICE_THINKING) {
        w->st = VOICE_SPEAKING;
        set_state(VOICE_SPEAKING);
    }
    if(V.out) rs_push(&w->rs, data, len, &V.pb);
}

static void on_text(void *ud, const char *text, size_t len)
{
    W *w = ud;
    cbj_t *r = cbj_parse(text, len);
    if(!r) return;
    const char *type = cbj_str(cbj_get(r, "type"), "");
    if(!strcmp(type, "cancelled") || !strcmp(type, "interrupted")) {
        bool cancelled = !strcmp(type, "cancelled");
        if(cancelled) w->discard = false;
        const cbj_t *tid = cbj_get(r, "turn_id");
        bool current = cancelled ? !tid || (int)cbj_num(tid, -1) == w->turn_id : tid && (int)cbj_num(tid, -1) == w->turn_id;
        if(current && !cancelled && w->st == VOICE_SPEAKING && (w->barging || w->barge_wait)) {
            /* confirmed barge-in: the old reply stops, the utterance being streamed is the new turn (its `session` follows) */
            bool streaming = w->barging;
            playback_stop();
            turn_reset(w);
            w->barging = false;
            w->barge_wait = 0;
            w->discard = false;
            if(streaming) {
                w->st = VOICE_HEARD;
                w->t_speech = now_ms();
                set_state(VOICE_HEARD);
                VIEW(V.view.err[0] = '\0'; V.view.turn++);
            }
            else {
                w->st = VOICE_THINKING;
                w->t_stop = now_ms();
                set_state(VOICE_THINKING);
            }
        }
        else if(current) {
            if(w->st == VOICE_THINKING || w->st == VOICE_SPEAKING) go_idle(w, 300);
            else {
                playback_stop();
                turn_reset(w);
            }
        }
    }
    else if(!strcmp(type, "error")) {
        const cbj_t *e = cbj_get(r, "error");
        set_err(e && e->type == CBJ_OBJ ? cbj_str(cbj_get(e, "message"), "server error") : cbj_str(e, "server error"));
        w->discard = false;
        if(w->st != VOICE_IDLE) go_idle(w, 300);
    }
    else if(w->discard) { /* a cancelled turn: nothing more is shown */ }
    else if(!strcmp(type, "session")) {
        w->turn_id = (int)cbj_num(cbj_get(r, "turn_id"), 0);
    }
    else if(!strcmp(type, "transcript_partial")) {
        VIEW(snprintf(V.view.transcript, sizeof V.view.transcript, "%s", cbj_str(cbj_get(r, "text"), "")); V.view.gen++);
    }
    else if(!strcmp(type, "transcript")) {
        VIEW(snprintf(V.view.transcript, sizeof V.view.transcript, "%s", cbj_str(cbj_get(r, "text"), "")); V.view.reply[0] = '\0'; V.view.gen++);
    }
    else if(!strcmp(type, "tts_start")) {
        if((int)cbj_num(cbj_get(r, "turn_id"), -1) == w->turn_id && (w->st == VOICE_THINKING || w->st == VOICE_SPEAKING)) {
            sent_start(&w->sent, (int)cbj_num(cbj_get(r, "sentence_seq"), 0), (uint32_t)cbj_num(cbj_get(r, "bytes"), 0));
            atomic_store(&V.sent_open, true);
            w->t_last = now_ms();
        }
    }
    else if(!strcmp(type, "tts_end")) {
        if(sent_open(&w->sent)) {
            int seq = (int)cbj_num(cbj_get(r, "sentence_seq"), -1);
            uint32_t bytes = (uint32_t)cbj_num(cbj_get(r, "bytes"), 0);
            if(!sent_end(&w->sent, seq, bytes))
                printf("voice: warning: sentence %d byte mismatch (got %u of %u, server says %u)\n", seq, w->sent.got, w->sent.expect, bytes);
            atomic_store(&V.sent_open, false);
            w->t_last = now_ms();
        }
    }
    else if(!strcmp(type, "ignored")) {
        if(w->st == VOICE_THINKING) go_idle(w, 200);
    }
    else if(!strcmp(type, "token")) {
        const char *t = cbj_str(cbj_get(r, "text"), "");
        VIEW(size_t have = strlen(V.view.reply), n = strlen(t);
             if(have + n + 1 < sizeof V.view.reply) memcpy(V.view.reply + have, t, n + 1);
             V.view.gen++);
    }
    else if(!strcmp(type, "done")) {
        const cbj_t *t = cbj_get(r, "timings");
        const char *reply = cbj_str(cbj_get(r, "text"), "");
        VIEW(V.view.have_timings = t != NULL;
             V.view.asr_ms = cbj_num(cbj_get(t, "asr_ms"), 0); V.view.ttft_ms = cbj_num(cbj_get(t, "llm_ttft_ms"), 0);
             V.view.llm_ms = cbj_num(cbj_get(t, "llm_ms"), 0); V.view.tts_ms = cbj_num(cbj_get(t, "tts_ms"), 0);
             if(*reply) snprintf(V.view.reply, sizeof V.view.reply, "%s", reply);
             V.view.gen++);
        w->done_seen = true;
        w->t_done = now_ms();
        w->done_queued = pcm_ring_avail(&V.pb);
    }
    cbj_free(r);
}

static void send_json(W *w, const char *s) { ws_client_send(w->ws, WS_OP_TEXT, s, strlen(s)); }

static void send_start(W *w, bool barge)
{
    char model[64], msg[192];
    pthread_mutex_lock(&V.cmd_mu);
    snprintf(model, sizeof model, "%s", V.model);
    pthread_mutex_unlock(&V.cmd_mu);
    snprintf(msg, sizeof msg, "{\"type\":\"start\",\"protocol\":2,\"model\":\"%s\"%s}", model, barge ? ",\"barge_in\":true" : "");
    send_json(w, msg);
    w->discard = false;
}

static double rms_of(const int16_t *f)
{
    double a = 0;
    for(int i = 0; i < FRAME; i++) a += (double)f[i] * f[i];
    return sqrt(a / FRAME);
}

static void send_pre_roll(W *w)
{
    int first = (w->pre_head - w->pre_n + PRE_FRAMES) % PRE_FRAMES;
    for(int i = 0; i < w->pre_n; i++) ws_client_send(w->ws, WS_OP_BIN, w->pre[(first + i) % PRE_FRAMES], sizeof w->pre[0]);
}

static void pre_push(W *w, const int16_t *f)
{
    memcpy(w->pre[w->pre_head], f, sizeof w->pre[0]);
    w->pre_head = (w->pre_head + 1) % PRE_FRAMES;
    if(w->pre_n < PRE_FRAMES) w->pre_n++;
}

/* SPEAKING with STUDIO_BARGE_IN=1: speech well above the idle threshold is streamed as a barge-in attempt. The server
   confirms it (`interrupted`) or drops it silently, in which case the reply keeps playing. */
static void on_barge_frame(W *w, const int16_t *f, uint32_t now)
{
    double r = rms_of(f);
    pre_push(w, f);
    if(w->barge_wait && (int32_t)(now - w->barge_wait) < 0) return;
    w->barge_wait = 0;
    if(!w->barging) {
        w->above = r > w->thr * BARGE_THR_MULT ? w->above + 1 : 0;
        if(w->above < BARGE_FRAMES || !w->open) return;
        send_start(w, true);
        send_pre_roll(w);
        w->barging = true;
        w->below = 0;
        w->utt_frames = w->pre_n;
        return;
    }
    ws_client_send(w->ws, WS_OP_BIN, f, sizeof w->pre[0]);
    w->utt_frames++;
    w->below = r < w->thr ? w->below + 1 : 0;
    if(w->below >= END_FRAMES || w->utt_frames >= MAX_UTT_FRAMES) {
        send_json(w, "{\"type\":\"stop\"}");
        w->barging = false;
        w->above = 0;
        w->barge_wait = now + BARGE_WAIT_MS;
        if(!w->barge_wait) w->barge_wait = 1;
    }
}

static void do_cancel(W *w)
{
    if(w->open) send_json(w, "{\"type\":\"cancel\"}");
    bool turn = w->st == VOICE_THINKING || w->st == VOICE_SPEAKING || w->st == VOICE_HEARD;
    playback_stop();
    turn_reset(w);
    if(turn) w->discard = true;
    go_idle(w, 300);
}

static void on_frame(W *w, int16_t *f)
{
    bool muted = atomic_load(&V.muted);
    uint32_t now = now_ms();
    if(muted) {
        if(w->st == VOICE_HEARD) do_cancel(w);   /* no half-sent utterances after a mute */
        if(w->barging) {
            send_json(w, "{\"type\":\"stop\"}");
            w->barging = false;
        }
        w->above = w->below = w->pre_n = 0;
        atomic_store(&V.level, 0);
        return;
    }
    if(w->st == VOICE_SPEAKING && w->barge_in && w->calib_n >= CALIB_FRAMES) {
        on_barge_frame(w, f, now);
        return;
    }
    if(w->st != VOICE_IDLE && w->st != VOICE_HEARD) return;   /* gated while thinking or speaking */
    if(w->st == VOICE_IDLE && (int32_t)(now - w->t_hold) < 0) return;
    double r = rms_of(f);
    if(w->calib_n < CALIB_FRAMES) {
        w->calib_sum += r;
        if(++w->calib_n == CALIB_FRAMES) {
            double floor_ = w->calib_sum / CALIB_FRAMES;
            const char *mn = getenv("STUDIO_VAD_MIN");
            double mi = mn ? atof(mn) : MIN_THRESHOLD;
            w->thr = floor_ * 3 > mi ? floor_ * 3 : mi;
            VIEW(V.view.calibrated = true; V.view.threshold = w->thr);
            printf("voice: noise floor %.0f, threshold %.0f\n", floor_, w->thr);
            fflush(stdout);
        }
        return;
    }
    if(w->st == VOICE_IDLE) {
        pre_push(w, f);
        w->above = r > w->thr ? w->above + 1 : 0;
        if(w->above >= START_FRAMES && w->open) {
            send_start(w, false);
            send_pre_roll(w);
            w->st = VOICE_HEARD;
            w->t_speech = now_ms();
            w->below = 0;
            w->utt_frames = w->pre_n;
            set_state(VOICE_HEARD);
            VIEW(V.view.err[0] = '\0'; V.view.turn++);
        }
        return;
    }
    /* HEARD */
    ws_client_send(w->ws, WS_OP_BIN, f, sizeof w->pre[0]);
    w->utt_frames++;
    w->below = r < w->thr ? w->below + 1 : 0;
    int lv = (int)(r / 6000.0 * 255.0);
    atomic_store(&V.level, lv > 255 ? 255 : lv);
    if(w->below >= END_FRAMES || w->utt_frames >= MAX_UTT_FRAMES) {
        send_json(w, "{\"type\":\"stop\"}");
        w->st = VOICE_THINKING;
        w->t_stop = now;
        turn_reset(w);
        atomic_store(&V.level, 0);
        set_state(VOICE_THINKING);
    }
}

static void load_injection(W *w)
{
    const char *path = getenv("STUDIO_VOICE_WAV");
    if(!path) return;
    FILE *f = fopen(path, "rb");
    if(!f) return;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *b = n > 0 ? malloc((size_t)n) : NULL;
    if(b && fread(b, 1, (size_t)n, f) == (size_t)n) w->inj = wav_decode(b, (size_t)n, RATE, &w->inj_n, NULL);
    free(b);
    fclose(f);
}

static void *voice_main(void *arg)
{
    (void)arg;
    W w = { .st = VOICE_IDLE };
    const char *barge_env = getenv("STUDIO_BARGE_IN");
    w.barge_in = barge_env && barge_env[0] == '1';
    w.t_open = now_ms();
    rs_init(&w.rs, RATE, V.out ? (int)(V.out_rate + 0.5) : RATE);
    turn_reset(&w);
    load_injection(&w);
    char url[512];
    net_ws_url("/v1/voice/stream", url, sizeof url);
    w.ws = ws_client_create(url, on_text, &w);
    if(w.ws) {
        ws_client_set_binary(w.ws, on_bin);
        ws_client_set_open(w.ws, on_open);
    }
    /* on_bin and on_open get the same user data as on_text */
    int16_t frame[FRAME];
    while(!atomic_load(&V.stop)) {
        uint32_t now = now_ms();
        ws_state_t ws = w.ws ? ws_client_poll(w.ws, now) : WS_CLOSED;
        now = now_ms();
        bool was_open = w.open;
        w.open = ws == WS_OPEN && w.open;
        if(ws != WS_OPEN) w.open = false;
        if(was_open && !w.open && w.st != VOICE_IDLE) {
            set_err("connection lost");
            playback_stop();
            go_idle(&w, 500);
        }
        VIEW(V.view.connected = w.open);

        if(atomic_exchange(&V.cancel, false)) do_cancel(&w);

        /* turn end and timeouts */
        bool live = w.st == VOICE_THINKING || w.st == VOICE_SPEAKING;
        if(live && w.sent.starts && turn_finished(w.done_seen, &w.sent, pcm_ring_avail(&V.pb) == 0)) {
            if(!w.t_fin) w.t_fin = now;
            else if(now - w.t_fin >= SPEAKER_TAIL_MS) {
                printf("voice: turn=%d speech_start=0 stop=%lld first_pcm=%lld drained=%lld bytes=%u/%u\n", w.turn_id,
                       (long long)(int32_t)(w.t_stop - w.t_speech), w.t_first_pcm ? (long long)(int32_t)(w.t_first_pcm - w.t_speech) : -1LL,
                       (long long)(int32_t)(now - w.t_speech), w.sent.tot_got, w.sent.tot_expect);
                fflush(stdout);
                playback_stop();
                go_idle(&w, 500);
            }
        }
        else w.t_fin = 0;
        /* the speaker callback stopped (device unplugged): leave SPEAKING once the queued audio had time to play plus slack */
        if(w.st == VOICE_SPEAKING && w.done_seen && !sent_open(&w.sent)
           && now - w.t_done > (uint32_t)((double)w.done_queued * 1000.0 / (V.out_rate > 0 ? V.out_rate : RATE)) + SPEAKER_HANG_MS) {
            set_err("the speaker stopped");
            playback_stop();
            go_idle(&w, 200);
        }
        if(w.st == VOICE_THINKING && w.done_seen && !w.sent.starts && now - w.t_done > 300) go_idle(&w, 200);
        if(w.st == VOICE_THINKING && !w.done_seen && !w.sent.starts && now - w.t_stop > 60000) { set_err("the box did not answer"); go_idle(&w, 200); }
        if(live && (!w.done_seen || sent_open(&w.sent)) && w.sent.starts && now - w.t_last > STALL_MS) { set_err("the reply stalled"); playback_stop(); go_idle(&w, 200); }

        /* mic frames */
        for(;;) {
            unsigned r = atomic_load_explicit(&V.rpos, memory_order_relaxed);
            unsigned wp = atomic_load_explicit(&V.wpos, memory_order_acquire);
            if(wp - r < FRAME) break;
            for(int i = 0; i < FRAME; i++) frame[i] = V.ring[(r + (unsigned)i) & (RING_N - 1)];
            atomic_store_explicit(&V.rpos, r + FRAME, memory_order_release);
            if(w.inj && !w.injected && !w.injecting && w.st == VOICE_IDLE && w.calib_n >= CALIB_FRAMES && now - w.t_open > 2500) w.injecting = true;
            if(w.injecting) {
                for(int i = 0; i < FRAME; i++) frame[i] = w.inj_pos < w.inj_n ? w.inj[w.inj_pos++] : 0;
                if(w.inj_pos >= w.inj_n + 16000) { w.injecting = false; w.injected = true; }
            }
            on_frame(&w, frame);
        }
        usleep(4000);
    }
    playback_stop();
    ws_client_destroy(w.ws);
    free(w.inj);
    return NULL;
}

/* ---------- lifecycle ---------- */

void voice_open(void)
{
    if(V.running) return;
    atomic_store(&V.stop, false);
    atomic_store(&V.cancel, false);
    atomic_store(&V.level, 0);
    atomic_store(&V.rpos, 0);
    atomic_store(&V.wpos, 0);
    pcm_ring_init(&V.pb);
    atomic_store(&V.primed, false);
    atomic_store(&V.sent_open, false);
    pthread_mutex_lock(&V.cmd_mu);
    if(!V.model[0]) snprintf(V.model, sizeof V.model, "qwen3-vl");
    pthread_mutex_unlock(&V.cmd_mu);
    pthread_mutex_lock(&V.view_mu);
    bool muted = atomic_load(&V.muted);
    memset(&V.view, 0, sizeof V.view);
    V.view.muted = muted;
    pthread_mutex_unlock(&V.view_mu);
    audio_open();
    V.running = pthread_create(&V.th, NULL, voice_main, NULL) == 0;
}

void voice_close(void)
{
    if(!V.running) return;
    atomic_store(&V.stop, true);
    pthread_join(V.th, NULL);
    V.running = false;
    audio_close();
    atomic_store(&V.level, 0);
}
