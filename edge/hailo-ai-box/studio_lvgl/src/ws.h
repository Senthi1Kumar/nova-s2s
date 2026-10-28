#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A minimal RFC 6455 client (copied from cb_lvgl, extended with sends of any length, binary messages and an open hook): ws:// only (loopback or LAN), non-blocking, polled from an
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
/* Binary messages (reassembled); the pointer is valid only during the callback. */
typedef void (*ws_on_bin_t)(void *ud, const uint8_t *data, size_t len);
/* Called each time the connection reaches OPEN (also after a reconnect): the place to send the first message. */
typedef void (*ws_on_open_t)(void *ud);

typedef struct ws_client ws_client_t;
ws_client_t *ws_client_create(const char *url, ws_on_text_t on_text, void *ud);   /* NULL for a bad url */
void ws_client_set_binary(ws_client_t *c, ws_on_bin_t on_bin);
void ws_client_set_open(ws_client_t *c, ws_on_open_t on_open);
void ws_client_set_auto_reconnect(ws_client_t *c, bool on);   /* default on; off = one connection attempt, then CLOSED for good */
bool ws_client_send(ws_client_t *c, int opcode, const void *data, size_t len);   /* any length; false if not open or the send fails */
ws_state_t ws_client_poll(ws_client_t *c, uint32_t now_ms);
void ws_client_reconnect(ws_client_t *c);   /* drop the connection now (e.g. a seq gap) */
const char *ws_client_error(const ws_client_t *c);
void ws_client_destroy(ws_client_t *c);
