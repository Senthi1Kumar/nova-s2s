// Nova realtime device: mic -> Pi, Pi -> speaker. Audio I/O only.
// Contract: docs/api/realtime.md (nova-realtime/1).
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "protocol_examples_common.h"
#include "esp_websocket_client.h"
#include "driver/i2s_std.h"
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "board.h"

#define RATE 16000
#define FRAME_SAMPLES (RATE / 50)                          // 20 ms
#define PREROLL_SAMPLES (RATE * CONFIG_NOVA_PREROLL_MS / 1000)
#define RX_BUF_BYTES (96 * 1024)                           // one JSON delta (<=200 ms), reassembled
#define PLAY_QUEUE_DEPTH 128                               // decoded deltas, ~25 s at 200 ms each

#if CONFIG_NOVA_WAKE_SERVER
#define WAKE_MODE "server"
#else
#define WAKE_MODE "device"
#endif
#if CONFIG_NOVA_ECHO_CANCELLATION
#define AEC_JSON "true"
#else
#define AEC_JSON "false"
#endif

static const char *TAG = "nova";
extern const char nova_cert_pem_start[] asm("_binary_nova_cert_pem_start");

static esp_websocket_client_handle_t s_ws;
static i2s_chan_handle_t s_mic, s_spk;
typedef struct { uint8_t *pcm; size_t len; } play_chunk_t;
static QueueHandle_t s_play;              // decoded deltas waiting for the speaker
static int s_generation = -1;             // generation_id of the reply being played
static char *s_rx;                       // text frame reassembly

static volatile bool s_idle = true;
static volatile bool s_arm_pending = false;
static volatile bool s_playing = false;
static volatile bool s_resp_done = false;

static int16_t s_ring[PREROLL_SAMPLES];  // last N ms of mic audio
static size_t s_ring_pos;

static void send_json(const char *json) {
    if (esp_websocket_client_is_connected(s_ws))
        esp_websocket_client_send_text(s_ws, json, strlen(json), portMAX_DELAY);
}

static bool should_stream(void) {
#if CONFIG_NOVA_WAKE_SERVER
    return true;
#else
    return s_arm_pending || !s_idle;
#endif
}

// Call from a WakeNet detection or a button: the device owns the wake word.
// Runs on the mic task, so the ring buffer is not being written concurrently.
void nova_local_wake(void) {
#if CONFIG_NOVA_ECHO_CANCELLATION
    if (s_playing) send_json("{\"type\":\"response.cancel\"}");
#endif
    s_arm_pending = true;
    send_json("{\"type\":\"session.arm\"}");
    // Pre-roll: the words spoken while the wake detector was still deciding.
    static int16_t tmp[PREROLL_SAMPLES];
    for (size_t i = 0; i < PREROLL_SAMPLES; i++)
        tmp[i] = s_ring[(s_ring_pos + i) % PREROLL_SAMPLES];
    esp_websocket_client_send_bin(s_ws, (const char *)tmp, sizeof tmp, portMAX_DELAY);
    ESP_LOGI(TAG, "local wake -> session.arm (+%d ms pre-roll)", CONFIG_NOVA_PREROLL_MS);
}

// Drop every queued chunk: the reply was cancelled (barge-in) or replaced.
static void flush_playback(void) {
    play_chunk_t c;
    while (xQueueReceive(s_play, &c, 0) == pdTRUE) free(c.pcm);
    if (s_playing) {
        s_playing = false;
        s_resp_done = false;
        send_json("{\"type\":\"playback.interrupted\"}");
    }
}

static void on_event_json(const char *text) {
    cJSON *ev = cJSON_Parse(text);
    if (!ev) return;
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(ev, "type"));
    if (!type) { cJSON_Delete(ev); return; }

    if (!strcmp(type, "nova.fsm")) {
        const char *st = cJSON_GetStringValue(
            cJSON_GetObjectItem(cJSON_GetObjectItem(ev, "fsm"), "state"));
        if (st) {
            s_idle = !strcmp(st, "IDLE");
            if (!s_idle) s_arm_pending = false;
            ESP_LOGI(TAG, "fsm %s", st);   // paint the round screen here
        }
    } else if (!strcmp(type, "response.audio.delta")) {
        const char *b64 = cJSON_GetStringValue(cJSON_GetObjectItem(ev, "delta"));
        int gen = (int)cJSON_GetNumberValue(cJSON_GetObjectItem(ev, "generation_id"));
        if (s_playing && gen != s_generation) flush_playback();   // stale reply
        s_generation = gen;
        if (b64) {
            size_t len = strlen(b64), cap = len * 3 / 4 + 4, n = 0;
            uint8_t *pcm = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
            if (pcm && mbedtls_base64_decode(pcm, cap, &n, (const uint8_t *)b64, len) == 0) {
                if (!s_playing) {
                    s_playing = true;
                    s_resp_done = false;
                    send_json("{\"type\":\"playback.started\"}");
                }
                // Never block this (websocket) task: it also carries the mic
                // uplink and barge-in. The play task owns the speaker.
                play_chunk_t c = { pcm, n };
                if (xQueueSend(s_play, &c, 0) != pdTRUE) {
                    ESP_LOGW(TAG, "play queue full; dropping %u bytes", (unsigned)n);
                    free(pcm);
                }
            } else {
                free(pcm);
            }
        }
    } else if (!strcmp(type, "response.done")) {
        const char *status = cJSON_GetStringValue(
            cJSON_GetObjectItem(cJSON_GetObjectItem(ev, "response"), "status"));
        if (status && !strcmp(status, "cancelled")) flush_playback();
        else s_resp_done = true;
    } else if (!strcmp(type, "error")) {
        ESP_LOGE(TAG, "server error: %s", text);
    }
    cJSON_Delete(ev);
}

static void ws_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    esp_websocket_event_data_t *d = data;
    if (id == WEBSOCKET_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "connected");
        char upd[256];
        snprintf(upd, sizeof upd,
                 "{\"type\":\"session.update\",\"session\":{\"input\":{\"wake\":\"%s\","
                 "\"echo_cancellation\":%s},\"audio\":{\"output\":{\"sample_rate\":%d}}}}",
                 WAKE_MODE, AEC_JSON, RATE);
        send_json(upd);
    } else if (id == WEBSOCKET_EVENT_DATA && (d->op_code == 0x1 || d->op_code == 0x0)) {
        // Large frames arrive in pieces; payload_offset says where each goes.
        if (d->payload_len >= RX_BUF_BYTES) { ESP_LOGW(TAG, "frame too big"); return; }
        memcpy(s_rx + d->payload_offset, d->data_ptr, d->data_len);
        if (d->payload_offset + d->data_len >= d->payload_len) {
            s_rx[d->payload_len] = 0;
            on_event_json(s_rx);
        }
    } else if (id == WEBSOCKET_EVENT_DISCONNECTED) {
        ESP_LOGW(TAG, "disconnected; client reconnects");
        s_idle = true;
    }
}

static void i2s_init(void) {
    i2s_chan_config_t mc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&mc, NULL, &s_mic));
    i2s_std_config_t m = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = BOARD_MIC_SCK, .ws = BOARD_MIC_WS,
                      .dout = I2S_GPIO_UNUSED, .din = BOARD_MIC_SD },
    };
    m.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_mic, &m));
    ESP_ERROR_CHECK(i2s_channel_enable(s_mic));

    i2s_chan_config_t sc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&sc, &s_spk, NULL));
    i2s_std_config_t s = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = BOARD_SPK_BCK, .ws = BOARD_SPK_LRCK,
                      .dout = BOARD_SPK_DIN, .din = I2S_GPIO_UNUSED },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_spk, &s));
    ESP_ERROR_CHECK(i2s_channel_enable(s_spk));
}

static void mic_task(void *arg) {
    static int32_t raw[FRAME_SAMPLES];
    static int16_t pcm[FRAME_SAMPLES];
    bool btn_was_down = false;
    for (;;) {
        size_t got = 0;
        i2s_channel_read(s_mic, raw, sizeof raw, &got, portMAX_DELAY);
        size_t n = got / sizeof(int32_t);
        for (size_t i = 0; i < n; i++) {
            pcm[i] = (int16_t)(raw[i] >> BOARD_MIC_SHIFT);
            s_ring[s_ring_pos] = pcm[i];
            s_ring_pos = (s_ring_pos + 1) % PREROLL_SAMPLES;
        }
        bool down = gpio_get_level(BOARD_WAKE_BUTTON) == 0;
        if (down && !btn_was_down) nova_local_wake();
        btn_was_down = down;
        if (should_stream() && esp_websocket_client_is_connected(s_ws))
            esp_websocket_client_send_bin(s_ws, (const char *)pcm, n * 2, pdMS_TO_TICKS(100));
    }
}

static void play_task(void *arg) {
    play_chunk_t c;
    for (;;) {
        if (xQueueReceive(s_play, &c, pdMS_TO_TICKS(40)) == pdTRUE) {
            size_t w;
            i2s_channel_write(s_spk, c.pcm, c.len, &w, portMAX_DELAY);
            free(c.pcm);
        } else if (s_playing && s_resp_done) {
            // Speaker is truly quiet now; the Pi holds its echo gate until this.
            s_playing = false;
            send_json("{\"type\":\"playback.stopped\"}");
        }
    }
}

void app_main(void) {
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect());

    gpio_set_direction(BOARD_WAKE_BUTTON, GPIO_MODE_INPUT);
    gpio_set_pull_mode(BOARD_WAKE_BUTTON, GPIO_PULLUP_ONLY);
    i2s_init();
    s_rx = heap_caps_malloc(RX_BUF_BYTES, MALLOC_CAP_SPIRAM);
    s_play = xQueueCreate(PLAY_QUEUE_DEPTH, sizeof(play_chunk_t));

    static char auth[160];
    snprintf(auth, sizeof auth, "Authorization: Bearer %s\r\n", CONFIG_NOVA_TOKEN);
    esp_websocket_client_config_t cfg = {
        .uri = CONFIG_NOVA_WS_URI,
        .headers = auth,
        .cert_pem = nova_cert_pem_start,     // trust exactly the Pi's cert
        .buffer_size = 4096,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = 10000,
    };
    s_ws = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_handler, NULL);
    esp_websocket_client_start(s_ws);

    xTaskCreatePinnedToCore(mic_task, "mic", 6144, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(play_task, "play", 4096, NULL, 5, NULL, 1);
}
