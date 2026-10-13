#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "live/ws.h"
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MSG_CAP (1u << 20)

/* ---------- pure helpers ---------- */

static uint32_t rol(uint32_t v, int n) { return (v << n) | (v >> (32 - n)); }

void ws_sha1(const uint8_t *data, size_t len, uint8_t out[20])
{
    uint32_t h[5] = { 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0 };
    uint64_t bits = (uint64_t)len * 8;
    size_t total = ((len + 8) / 64 + 1) * 64;
    for(size_t off = 0; off < total; off += 64) {
        uint8_t blk[64];
        for(int i = 0; i < 64; i++) {
            size_t k = off + (size_t)i;
            blk[i] = k < len ? data[k] : k == len ? 0x80 : 0;
        }
        if(off + 64 == total)
            for(int i = 0; i < 8; i++) blk[56 + i] = (uint8_t)(bits >> (56 - 8 * i));
        uint32_t w[80];
        for(int i = 0; i < 16; i++)
            w[i] = (uint32_t)blk[4 * i] << 24 | (uint32_t)blk[4 * i + 1] << 16 | (uint32_t)blk[4 * i + 2] << 8 | blk[4 * i + 3];
        for(int i = 16; i < 80; i++) w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for(int i = 0; i < 80; i++) {
            uint32_t f, k;
            if(i < 20) f = (b & c) | (~b & d), k = 0x5A827999;
            else if(i < 40) f = b ^ c ^ d, k = 0x6ED9EBA1;
            else if(i < 60) f = (b & c) | (b & d) | (c & d), k = 0x8F1BBCDC;
            else f = b ^ c ^ d, k = 0xCA62C1D6;
            uint32_t t = rol(a, 5) + f + e + k + w[i];
            e = d;
            d = c;
            c = rol(b, 30);
            b = a;
            a = t;
        }
        h[0] += a;
        h[1] += b;
        h[2] += c;
        h[3] += d;
        h[4] += e;
    }
    for(int i = 0; i < 5; i++)
        for(int j = 0; j < 4; j++) out[4 * i + j] = (uint8_t)(h[i] >> (24 - 8 * j));
}

void ws_base64(const uint8_t *in, size_t len, char *out)
{
    static const char t[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t o = 0;
    for(size_t i = 0; i < len; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16 | (i + 1 < len ? (uint32_t)in[i + 1] << 8 : 0) | (i + 2 < len ? in[i + 2] : 0);
        out[o++] = t[(v >> 18) & 63];
        out[o++] = t[(v >> 12) & 63];
        out[o++] = i + 1 < len ? t[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < len ? t[v & 63] : '=';
    }
    out[o] = '\0';
}

void ws_accept_for(const char *key, char out[29])
{
    char buf[128];
    snprintf(buf, sizeof buf, "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", key);
    uint8_t h[20];
    ws_sha1((const uint8_t *)buf, strlen(buf), h);
    ws_base64(h, 20, out);
}

int ws_decode(const uint8_t *b, size_t n, ws_hdr_t *h)
{
    if(n < 2) return 0;
    h->fin = (b[0] & 0x80) != 0;
    h->opcode = b[0] & 0x0F;
    if(b[1] & 0x80) return -1;
    uint64_t len = b[1] & 0x7F;
    size_t hdr = 2;
    if(len == 126) {
        if(n < 4) return 0;
        len = (uint64_t)b[2] << 8 | b[3];
        hdr = 4;
    }
    else if(len == 127) {
        if(n < 10) return 0;
        len = 0;
        for(int i = 0; i < 8; i++) len = len << 8 | b[2 + i];
        hdr = 10;
    }
    h->len = len;
    h->header = hdr;
    return n - hdr >= len ? 1 : 0;
}

size_t ws_encode_client(int opcode, const uint8_t *p, size_t len, uint32_t mask, uint8_t *out)
{
    out[0] = (uint8_t)(0x80 | opcode);
    out[1] = (uint8_t)(0x80 | len);
    uint8_t m[4] = { (uint8_t)(mask >> 24), (uint8_t)(mask >> 16), (uint8_t)(mask >> 8), (uint8_t)mask };
    memcpy(out + 2, m, 4);
    for(size_t i = 0; i < len; i++) out[6 + i] = p[i] ^ m[i % 4];
    return 6 + len;
}

bool ws_parse_url(const char *url, char *host, size_t host_cap, int *port, char *path, size_t path_cap)
{
    if(strncmp(url, "ws://", 5) != 0) return false;
    const char *h = url + 5, *slash = strchr(h, '/');
    size_t span = slash ? (size_t)(slash - h) : strlen(h);
    const char *colon = memchr(h, ':', span);
    size_t hl = colon ? (size_t)(colon - h) : span;
    if(hl == 0 || hl >= host_cap) return false;
    memcpy(host, h, hl);
    host[hl] = '\0';
    *port = colon ? atoi(colon + 1) : 80;
    snprintf(path, path_cap, "%s", slash ? slash : "/");
    return *port > 0 && *port < 65536;
}

/* ---------- client ---------- */

typedef enum { ST_IDLE, ST_CONNECT, ST_HANDSHAKE, ST_OPEN } st_t;

struct ws_client {
    char host[128], path[256], key[32], err[128];
    int port, fd;
    st_t st;
    uint32_t retry_at, backoff;
    ws_on_text_t on_text;
    void *ud;
    uint8_t *rx;
    size_t rx_len, rx_cap;
    char *msg;
    size_t msg_len, msg_cap;
};

static void drop(ws_client_t *c)
{
    if(c->fd >= 0) close(c->fd);
    c->fd = -1;
    c->st = ST_IDLE;
    c->rx_len = c->msg_len = 0;
}

static void fail(ws_client_t *c, const char *why, uint32_t now)
{
    drop(c);
    snprintf(c->err, sizeof c->err, "%s", why);
    c->retry_at = now + c->backoff;
    c->backoff = c->backoff * 2 > 5000 ? 5000 : c->backoff * 2;
}

ws_client_t *ws_client_create(const char *url, ws_on_text_t on_text, void *ud)
{
    ws_client_t *c = calloc(1, sizeof *c);
    if(!c) return NULL;
    if(!ws_parse_url(url, c->host, sizeof c->host, &c->port, c->path, sizeof c->path)) {
        free(c);
        return NULL;
    }
    c->fd = -1;
    c->backoff = 500;
    c->on_text = on_text;
    c->ud = ud;
    srand((unsigned)time(NULL) ^ (unsigned)(uintptr_t)c);
    return c;
}

void ws_client_destroy(ws_client_t *c)
{
    if(!c) return;
    if(c->fd >= 0) close(c->fd);
    free(c->rx);
    free(c->msg);
    free(c);
}

void ws_client_reconnect(ws_client_t *c)
{
    drop(c);
    c->retry_at = 0;
}

const char *ws_client_error(const ws_client_t *c) { return c->err; }

static bool start_connect(ws_client_t *c, uint32_t now)
{
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM }, *ai = NULL;
    char port[8];
    snprintf(port, sizeof port, "%d", c->port);
    if(getaddrinfo(c->host, port, &hints, &ai) != 0 || !ai) {
        fail(c, "cannot resolve host", now);
        return false;
    }
    c->fd = socket(ai->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int one = 1;
    setsockopt(c->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    int r = connect(c->fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    if(r != 0 && errno != EINPROGRESS) {
        fail(c, "connection refused", now);
        return false;
    }
    c->st = ST_CONNECT;
    return true;
}

static bool send_all(int fd, const void *p, size_t n)
{
    const uint8_t *b = p;
    for(int spins = 0; n && spins < 1000; spins++) {   /* handshake and control frames are tiny */
        ssize_t w = send(fd, b, n, MSG_NOSIGNAL);
        if(w < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if(w <= 0) return false;
        b += w;
        n -= (size_t)w;
    }
    return n == 0;
}

static bool send_handshake(ws_client_t *c)
{
    uint8_t raw[16];
    for(int i = 0; i < 16; i++) raw[i] = (uint8_t)rand();
    ws_base64(raw, 16, c->key);
    char req[640];
    int n = snprintf(req, sizeof req,
                     "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
                     c->path, c->host, c->port, c->key);
    return send_all(c->fd, req, (size_t)n);
}

static bool rx_reserve(ws_client_t *c, size_t extra)
{
    if(c->rx_len + extra <= c->rx_cap) return true;
    size_t cap = c->rx_cap ? c->rx_cap : 65536;
    while(cap < c->rx_len + extra) cap *= 2;
    if(cap > 2 * MSG_CAP) return false;
    uint8_t *p = realloc(c->rx, cap);
    if(!p) return false;
    c->rx = p;
    c->rx_cap = cap;
    return true;
}

static void control(ws_client_t *c, int op, const uint8_t *p, size_t n)
{
    uint8_t out[140];
    size_t len = ws_encode_client(op, p, n > 125 ? 125 : n, (uint32_t)rand(), out);
    send_all(c->fd, out, len);
}

/* false: the connection was dropped (error, close, or the callback asked for a reconnect) */
static bool handle_frames(ws_client_t *c, uint32_t now)
{
    size_t off = 0;
    for(;;) {
        ws_hdr_t h = { 0 };
        int r = ws_decode(c->rx + off, c->rx_len - off, &h);
        if(r < 0) {
            fail(c, "protocol error (masked server frame)", now);
            return false;
        }
        if(r == 0) {
            if(c->rx_len - off >= 2 && h.len > MSG_CAP) {
                fail(c, "message too large", now);
                return false;
            }
            break;
        }
        const uint8_t *p = c->rx + off + h.header;
        size_t n = (size_t)h.len;
        off += h.header + n;
        if(h.opcode == WS_OP_PING) control(c, WS_OP_PONG, p, n);
        else if(h.opcode == WS_OP_CLOSE) {
            control(c, WS_OP_CLOSE, p, n > 2 ? 2 : n);
            fail(c, "server closed", now);
            return false;
        }
        else if(h.opcode == WS_OP_TEXT || h.opcode == WS_OP_CONT) {
            if(c->msg_len + n + 1 > MSG_CAP) {
                fail(c, "message too large", now);
                return false;
            }
            if(c->msg_len + n + 1 > c->msg_cap) {
                size_t cap = c->msg_cap ? c->msg_cap : 65536;
                while(cap < c->msg_len + n + 1) cap *= 2;
                char *m = realloc(c->msg, cap);
                if(!m) return false;
                c->msg = m;
                c->msg_cap = cap;
            }
            memcpy(c->msg + c->msg_len, p, n);
            c->msg_len += n;
            if(h.fin) {
                c->msg[c->msg_len] = '\0';
                size_t len = c->msg_len;
                c->msg_len = 0;
                c->on_text(c->ud, c->msg, len);
                if(c->st != ST_OPEN) return false;
            }
        }
    }
    memmove(c->rx, c->rx + off, c->rx_len - off);
    c->rx_len -= off;
    return true;
}

static bool finish_handshake(ws_client_t *c, uint32_t now)
{
    c->rx[c->rx_len] = 0;
    char *end = strstr((char *)c->rx, "\r\n\r\n");
    if(!end) return true;   /* headers not complete yet */
    if(strncmp((char *)c->rx, "HTTP/1.1 101", 12) != 0) {
        fail(c, "upgrade refused", now);
        return false;
    }
    char accept[29];
    ws_accept_for(c->key, accept);
    *end = '\0';
    bool good = strstr((char *)c->rx, accept) != NULL;
    if(!good) {
        fail(c, "bad Sec-WebSocket-Accept", now);
        return false;
    }
    size_t used = (size_t)(end + 4 - (char *)c->rx);
    memmove(c->rx, c->rx + used, c->rx_len - used);
    c->rx_len -= used;
    c->st = ST_OPEN;
    c->backoff = 500;
    c->err[0] = '\0';
    return handle_frames(c, now);
}

ws_state_t ws_client_poll(ws_client_t *c, uint32_t now)
{
    if(c->st == ST_IDLE) {
        if((int32_t)(now - c->retry_at) < 0) return WS_CLOSED;
        if(!start_connect(c, now)) return WS_CLOSED;
    }
    if(c->st == ST_CONNECT) {
        struct pollfd p = { c->fd, POLLOUT, 0 };
        if(poll(&p, 1, 0) == 0) return WS_CONNECTING;
        int err = 0;
        socklen_t l = sizeof err;
        getsockopt(c->fd, SOL_SOCKET, SO_ERROR, &err, &l);
        if(err) {
            fail(c, err == ECONNREFUSED ? "connection refused" : "connect failed", now);
            return WS_CLOSED;
        }
        if(!send_handshake(c)) {
            fail(c, "handshake send failed", now);
            return WS_CLOSED;
        }
        c->st = ST_HANDSHAKE;
    }
    for(;;) {
        if(!rx_reserve(c, 16384 + 1)) {
            fail(c, "message too large", now);
            return WS_CLOSED;
        }
        ssize_t r = recv(c->fd, c->rx + c->rx_len, c->rx_cap - c->rx_len - 1, MSG_DONTWAIT);
        if(r > 0) {
            c->rx_len += (size_t)r;
            bool ok = c->st == ST_HANDSHAKE ? finish_handshake(c, now) : handle_frames(c, now);
            if(!ok) return WS_CLOSED;
            continue;
        }
        if(r == 0) {
            fail(c, "connection closed", now);
            return WS_CLOSED;
        }
        if(errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) break;
        fail(c, errno == ECONNREFUSED ? "connection refused" : "connection lost", now);
        return WS_CLOSED;
    }
    return c->st == ST_OPEN ? WS_OPEN : WS_CONNECTING;
}
