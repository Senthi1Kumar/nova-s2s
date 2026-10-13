#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A minimal RFC 6455 client for the observe stream: ws:// only (loopback or LAN), non-blocking, polled from an
   LVGL timer, reconnecting with backoff 0.5 -> 5 s. The handshake checks Sec-WebSocket-Accept; servers' frames
   must be unmasked; fragmented text is reassembled; ping gets a pong; close gets a close and a reconnect. */
enum { WS_OP_CONT = 0, WS_OP_TEXT = 1, WS_OP_BIN = 2, WS_OP_CLOSE = 8, WS_OP_PING = 9, WS_OP_PONG = 10 };

typedef struct {
    bool fin;
    int opcode;
    uint64_t len;    /* payload bytes */
    size_t header;   /* header bytes before the payload */
} ws_hdr_t;

void ws_sha1(const uint8_t *data, size_t len, uint8_t out[20]);
void ws_base64(const uint8_t *in, size_t len, char *out);   /* NUL-terminated, 4 * ceil(len / 3) chars */
void ws_accept_for(const char *key, char out[29]);
int ws_decode(const uint8_t *buf, size_t len, ws_hdr_t *h);   /* 1 frame complete, 0 need more, -1 protocol error */
size_t ws_encode_client(int opcode, const uint8_t *payload, size_t len, uint32_t mask, uint8_t *out);   /* len <= 125 */
bool ws_parse_url(const char *url, char *host, size_t host_cap, int *port, char *path, size_t path_cap);

typedef enum { WS_CLOSED, WS_CONNECTING, WS_OPEN } ws_state_t;
typedef void (*ws_on_text_t)(void *ud, const char *text, size_t len);

typedef struct ws_client ws_client_t;
ws_client_t *ws_client_create(const char *url, ws_on_text_t on_text, void *ud);   /* NULL for a bad url */
ws_state_t ws_client_poll(ws_client_t *c, uint32_t now_ms);
void ws_client_reconnect(ws_client_t *c);   /* drop the connection now (e.g. a seq gap) */
const char *ws_client_error(const ws_client_t *c);
void ws_client_destroy(ws_client_t *c);
