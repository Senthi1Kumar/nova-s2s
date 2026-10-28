#define _GNU_SOURCE
#include "voice.h"
#include "json.h"
#include "net.h"
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
#define CALIB_FRAMES 50                   /* first second of room noise */
#define MIN_THRESHOLD 350.0
#define MIC_GAIN 3                        /* XVF3800 channel 1 */
#define WAV_CAP (8u << 20)

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
    double in_rate, out_rate;
    int16_t ring[RING_N];
    atomic_uint rpos, wpos;
    double cap_phase, cap_prev;

    /* playback: PCM guarded by a mutex the callback only try-locks */
    pthread_mutex_t pb_mu;
    int16_t *pb;
    size_t pb_len;
    atomic_size_t pb_pos;
    atomic_bool pb_active;
} V = { .cmd_mu = PTHREAD_MUTEX_INITIALIZER, .view_mu = PTHREAD_MUTEX_INITIALIZER, .pb_mu = PTHREAD_MUTEX_INITIALIZER };

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
        if(V.in_ch > 1) v *= MIC_GAIN;
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

static int spk_cb(const void *input, void *output, unsigned long frames, const PaStreamCallbackTimeInfo *ti, PaStreamCallbackFlags fl, void *ud)
{
    (void)input; (void)ti; (void)fl; (void)ud;
    int16_t *o = output;
    int ch = V.out_ch;
    memset(o, 0, frames * (unsigned long)ch * sizeof *o);
    if(atomic_load(&V.pb_active) && pthread_mutex_trylock(&V.pb_mu) == 0) {
        size_t pos = atomic_load(&V.pb_pos), n = 0;
        double acc = 0;
        while(n < frames && pos < V.pb_len) {
            int16_t s = V.pb[pos++];
            acc += (double)s * s;
            for(int c = 0; c < ch; c++) o[n * (unsigned long)ch + (unsigned long)c] = s;
            n++;
        }
        atomic_store(&V.pb_pos, pos);
        if(pos >= V.pb_len) atomic_store(&V.pb_active, false);
        pthread_mutex_unlock(&V.pb_mu);
        double rms = n ? sqrt(acc / (double)n) : 0;
        int lv = (int)(rms / 5000.0 * 255.0);
        atomic_store(&V.level, lv > 255 ? 255 : lv);
    }
    else atomic_store(&V.level, 0);
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
        V.in_ch = ii->maxInputChannels >= 2 ? 2 : 1;
        V.in_idx = V.in_ch > 1 ? 1 : 0;
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
    printf("voice: mic %s (%s, ch%d@%.0f)  speaker %s (@%.0f x%d)\n", V.in ? ii->name : "none", V.in ? "open" : "-", V.in_idx, V.in_rate,
           V.out ? oi->name : "none", V.out_rate, V.out_ch);
    fflush(stdout);
}

static void audio_close(void)
{
    if(V.in) { Pa_StopStream(V.in); Pa_CloseStream(V.in); V.in = NULL; }
    if(V.out) { Pa_StopStream(V.out); Pa_CloseStream(V.out); V.out = NULL; }
    Pa_Terminate();
}

static void playback_stop(void)
{
    atomic_store(&V.pb_active, false);
    pthread_mutex_lock(&V.pb_mu);
    free(V.pb);
    V.pb = NULL;
    V.pb_len = 0;
    atomic_store(&V.pb_pos, 0);
    pthread_mutex_unlock(&V.pb_mu);
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
    voice_state_t st;
    uint32_t t_open, t_stop, t_done, t_wav, t_hold;
    /* WAV assembly */
    uint8_t *wav;
    size_t wav_len, wav_cap, wav_total;
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

static void wav_reset(W *w)
{
    w->wav_len = w->wav_total = 0;
}

static void go_idle(W *w, uint32_t holdoff_ms)
{
    w->st = VOICE_IDLE;
    w->t_hold = now_ms() + holdoff_ms;
    w->above = w->below = w->pre_n = 0;
    w->done_seen = false;
    set_state(VOICE_IDLE);
    atomic_store(&V.level, 0);
    atomic_store(&V.rpos, atomic_load(&V.wpos));   /* drop audio captured while gated */
}

static void begin_playback(W *w)
{
    size_t n = 0;
    int16_t *pcm = wav_decode(w->wav, w->wav_len, V.out ? V.out_rate : RATE, &n, NULL);
    wav_reset(w);
    if(!pcm || !V.out) {
        free(pcm);
        set_err(V.out ? "reply audio not understood" : "no speaker: reply shown as text");
        go_idle(w, 200);
        return;
    }
    playback_stop();
    pthread_mutex_lock(&V.pb_mu);
    V.pb = pcm;
    V.pb_len = n;
    atomic_store(&V.pb_pos, 0);
    pthread_mutex_unlock(&V.pb_mu);
    atomic_store(&V.pb_active, true);
    w->st = VOICE_SPEAKING;
    set_state(VOICE_SPEAKING);
}

static void on_bin(void *ud, const uint8_t *data, size_t len)
{
    W *w = ud;
    if(w->discard || w->st == VOICE_IDLE || w->st == VOICE_HEARD) return;
    if(w->wav_len == 0) {
        if(len < 12 || memcmp(data, "RIFF", 4)) return;
        w->wav_total = le32(data + 4) + 8u;
        if(w->wav_total < 44 || w->wav_total > WAV_CAP) w->wav_total = 0;   /* unknown: play after a quiet gap */
    }
    if(w->wav_len + len > WAV_CAP) return;
    if(w->wav_len + len > w->wav_cap) {
        w->wav_cap = (w->wav_len + len) * 2;
        w->wav = realloc(w->wav, w->wav_cap);
    }
    memcpy(w->wav + w->wav_len, data, len);
    w->wav_len += len;
    w->t_wav = now_ms();
    if(w->wav_total && w->wav_len >= w->wav_total) begin_playback(w);
}

static void on_text(void *ud, const char *text, size_t len)
{
    W *w = ud;
    cbj_t *r = cbj_parse(text, len);
    if(!r) return;
    const char *type = cbj_str(cbj_get(r, "type"), "");
    if(!strcmp(type, "cancelled")) w->discard = false;
    else if(!strcmp(type, "error")) {
        const cbj_t *e = cbj_get(r, "error");
        set_err(e && e->type == CBJ_OBJ ? cbj_str(cbj_get(e, "message"), "server error") : cbj_str(e, "server error"));
        w->discard = false;
        if(w->st != VOICE_IDLE) go_idle(w, 300);
    }
    else if(w->discard) { /* a cancelled turn: nothing more is shown */ }
    else if(!strcmp(type, "transcript")) {
        VIEW(snprintf(V.view.transcript, sizeof V.view.transcript, "%s", cbj_str(cbj_get(r, "text"), "")); V.view.reply[0] = '\0'; V.view.gen++);
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
        if(!*reply) go_idle(w, 200);
    }
    cbj_free(r);
}

static void send_json(W *w, const char *s) { ws_client_send(w->ws, WS_OP_TEXT, s, strlen(s)); }

static void send_start(W *w)
{
    char model[64], msg[160];
    pthread_mutex_lock(&V.cmd_mu);
    snprintf(model, sizeof model, "%s", V.model);
    pthread_mutex_unlock(&V.cmd_mu);
    snprintf(msg, sizeof msg, "{\"type\":\"start\",\"model\":\"%s\"}", model);
    send_json(w, msg);
    w->discard = false;
}

static double rms_of(const int16_t *f)
{
    double a = 0;
    for(int i = 0; i < FRAME; i++) a += (double)f[i] * f[i];
    return sqrt(a / FRAME);
}

static void do_cancel(W *w)
{
    if(w->open) send_json(w, "{\"type\":\"cancel\"}");
    bool turn = w->st == VOICE_THINKING || w->st == VOICE_SPEAKING || w->st == VOICE_HEARD;
    playback_stop();
    wav_reset(w);
    if(turn) w->discard = true;
    go_idle(w, 300);
}

static void on_frame(W *w, int16_t *f)
{
    bool muted = atomic_load(&V.muted);
    uint32_t now = now_ms();
    if(muted) {
        if(w->st == VOICE_HEARD) do_cancel(w);   /* no half-sent utterances after a mute */
        w->above = w->below = w->pre_n = 0;
        atomic_store(&V.level, 0);
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
        memcpy(w->pre[w->pre_head], f, sizeof w->pre[0]);
        w->pre_head = (w->pre_head + 1) % PRE_FRAMES;
        if(w->pre_n < PRE_FRAMES) w->pre_n++;
        w->above = r > w->thr ? w->above + 1 : 0;
        if(w->above >= START_FRAMES && w->open) {
            send_start(w);
            int first = (w->pre_head - w->pre_n + PRE_FRAMES) % PRE_FRAMES;
            for(int i = 0; i < w->pre_n; i++) ws_client_send(w->ws, WS_OP_BIN, w->pre[(first + i) % PRE_FRAMES], sizeof w->pre[0]);
            w->st = VOICE_HEARD;
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
        w->done_seen = false;
        wav_reset(w);
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
    w.t_open = now_ms();
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

        /* timeouts */
        if(w.st == VOICE_THINKING && w.done_seen && w.wav_len == 0 && now - w.t_done > 6000) { set_err("no audio in the reply"); go_idle(&w, 200); }
        if(w.st == VOICE_THINKING && w.wav_len && !w.wav_total && now - w.t_wav > 300) begin_playback(&w);
        if(w.st == VOICE_THINKING && !w.done_seen && now - w.t_stop > 60000) { set_err("the box did not answer"); go_idle(&w, 200); }
        if(w.st == VOICE_SPEAKING && !atomic_load(&V.pb_active)) {
            playback_stop();
            go_idle(&w, 500);
        }

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
    free(w.wav);
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
