#include "live/ws.h"
#include <stdio.h>
#include <string.h>

static int fails;
#define CHECK(c) do { if(!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while(0)

static void hex(const uint8_t *d, size_t n, char *out)
{
    for(size_t i = 0; i < n; i++) sprintf(out + 2 * i, "%02x", d[i]);
}

static void sha1_and_base64(void)
{
    uint8_t h[20];
    char x[41];
    ws_sha1((const uint8_t *)"abc", 3, h);
    hex(h, 20, x);
    CHECK(!strcmp(x, "a9993e364706816aba3e25717850c26c9cd0d89d"));
    const char *two = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    ws_sha1((const uint8_t *)two, strlen(two), h);
    hex(h, 20, x);
    CHECK(!strcmp(x, "84983e441c3bd26ebaae4aa1f95129e5e54670f1"));
    char b[16];
    ws_base64((const uint8_t *)"foob", 4, b);
    CHECK(!strcmp(b, "Zm9vYg=="));
    char acc[29];
    ws_accept_for("dGhlIHNhbXBsZSBub25jZQ==", acc);   /* RFC 6455 section 1.3 */
    CHECK(!strcmp(acc, "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="));
}

static void decode(void)
{
    ws_hdr_t h;
    const uint8_t small[] = { 0x81, 0x05, 'h', 'e', 'l', 'l', 'o' };
    CHECK(ws_decode(small, sizeof small, &h) == 1);
    CHECK(h.fin && h.opcode == WS_OP_TEXT && h.len == 5 && h.header == 2);
    CHECK(ws_decode(small, 4, &h) == 0);   /* payload incomplete */
    CHECK(ws_decode(small, 1, &h) == 0);   /* header incomplete */

    uint8_t mid[4 + 300] = { 0x01, 126, 0x01, 0x2c };   /* not final, 300 bytes */
    CHECK(ws_decode(mid, sizeof mid, &h) == 1);
    CHECK(!h.fin && h.opcode == WS_OP_TEXT && h.len == 300 && h.header == 4);

    uint8_t big[10] = { 0x82, 127, 0, 0, 0, 0, 0, 1, 0, 0 };   /* 65536 bytes announced */
    CHECK(ws_decode(big, sizeof big, &h) == 0);
    CHECK(h.len == 65536 && h.header == 10);

    const uint8_t masked[] = { 0x81, 0x81, 1, 2, 3, 4, 'x' };   /* servers must not mask */
    CHECK(ws_decode(masked, sizeof masked, &h) == -1);
}

static void encode(void)
{
    uint8_t out[64];
    size_t n = ws_encode_client(WS_OP_PONG, (const uint8_t *)"ab", 2, 0x01020304u, out);
    CHECK(n == 8);
    CHECK(out[0] == 0x8A && out[1] == (0x80 | 2));
    CHECK(out[2] == 1 && out[3] == 2 && out[4] == 3 && out[5] == 4);
    CHECK(out[6] == ('a' ^ 1) && out[7] == ('b' ^ 2));
    n = ws_encode_client(WS_OP_CLOSE, NULL, 0, 0, out);
    CHECK(n == 6 && out[0] == 0x88 && out[1] == 0x80);
}

static void url(void)
{
    char host[64], path[64];
    int port;
    CHECK(ws_parse_url("ws://127.0.0.1:8766/v1/observe", host, sizeof host, &port, path, sizeof path));
    CHECK(!strcmp(host, "127.0.0.1") && port == 8766 && !strcmp(path, "/v1/observe"));
    CHECK(ws_parse_url("ws://pi.local/v1/observe", host, sizeof host, &port, path, sizeof path));
    CHECK(port == 80 && !strcmp(host, "pi.local"));
    CHECK(!ws_parse_url("wss://pi.local:8766/v1/observe", host, sizeof host, &port, path, sizeof path));
}

int main(void)
{
    sha1_and_base64();
    decode();
    encode();
    url();
    if(fails) return 1;
    printf("ok\n");
    return 0;
}
