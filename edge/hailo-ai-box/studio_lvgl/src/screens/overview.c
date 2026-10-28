#include "app.h"
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>

#define N_SVC 4
#define N_TILES 4

static struct {
    lv_obj_t *cnt[4], *svc_state[N_SVC], *svc_desc[N_SVC];
    lv_obj_t *tv[N_TILES], *tb[N_TILES], *tsub[N_TILES];
    lv_obj_t *insp[4], *conn_lan;
    uint32_t seen_seq, seen_models, seen_rows;
} O;

static const struct { const char *name, *desc, *path, *tag; cb_color_t color; int goto_page; } k_svc[N_SVC] = {
    { "Voice Intelligence", "ASR \xC2\xB7 Conversational AI \xC2\xB7 TTS", "/v1/voice/stream", "V", CB_CYAN, 1 },
    { "Computer Vision", "Detection \xC2\xB7 Pose \xC2\xB7 Segmentation \xC2\xB7 Lanes \xC2\xB7 DMS \xC2\xB7 OCR", "/v1/vision/detect", "C", CB_VIOLET, 2 },
    { "Language Models", "Local chat \xC2\xB7 Streaming responses", "/v1/chat/completions", "L", CB_GREEN, 3 },
    { "Vision-Language", "Ask about a camera frame", "/v1/vision/ask", "VL", CB_ORANGE, 2 },
};

static int svc_of(const char *modality)
{
    if(!strcmp(modality, "asr") || !strcmp(modality, "tts")) return 0;
    if(!strcmp(modality, "vision")) return 1;
    if(!strcmp(modality, "chat")) return 2;
    if(!strcmp(modality, "vlm")) return 3;
    return -1;
}

static void lan_ip(char *out, size_t cap)
{
    snprintf(out, cap, "<pi-address>");
    struct ifaddrs *ifa = NULL;
    if(getifaddrs(&ifa) != 0) return;
    for(struct ifaddrs *p = ifa; p; p = p->ifa_next) {
        if(!p->ifa_addr || p->ifa_addr->sa_family != AF_INET || (p->ifa_flags & 8 /* IFF_LOOPBACK */)) continue;
        struct in_addr a = ((struct sockaddr_in *)p->ifa_addr)->sin_addr;
        inet_ntop(AF_INET, &a, out, (socklen_t)cap);
        break;
    }
    freeifaddrs(ifa);
}

static void svc_click(lv_event_t *e)
{
    int pg = (int)(intptr_t)lv_event_get_user_data(e);
    if(pg >= 0) app_goto(pg);
}

static lv_obj_t *icon_box(lv_obj_t *parent, const char *tag, cb_color_t c)
{
    lv_obj_t *b = ui_plain(parent);
    lv_obj_set_size(b, 44, 44);
    lv_obj_set_style_radius(b, 10, 0);
    lv_obj_set_style_bg_color(b, cb_col(c), 0);
    lv_obj_set_style_bg_opa(b, 40, 0);
    lv_obj_set_style_border_color(b, cb_col(c), 0);
    lv_obj_set_style_border_opa(b, 120, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_t *l = ui_label(b, tag, CB_F_VALUE, c);
    lv_obj_center(l);
    return b;
}

static void build(lv_obj_t *page)
{
    lv_obj_t *counters = app_flow(page, 14);
    static const char *const cl[4] = { "AI Models Resident", "API Endpoints", "Connected Clients", "Requests / Sec" };
    for(int i = 0; i < 4; i++) {
        lv_obj_t *t = app_tile(counters, cl[i], &O.cnt[i], NULL, 24);
        lv_obj_set_style_text_font(O.cnt[i], cb_font(CB_F_SPEED), 0);
        (void)t;
    }

    lv_obj_t *reg_t = ui_label(page, "AI Service Registry", CB_F_TITLE, CB_TEXT_HI);
    lv_obj_set_style_pad_top(reg_t, 6, 0);
    for(int i = 0; i < N_SVC; i++) {
        lv_obj_t *r = ui_card(page);
        lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(r, 16, 0);
        lv_obj_set_style_pad_all(r, 14, 0);
        if(k_svc[i].goto_page >= 0) {
            lv_obj_set_clickable(r, true);
            lv_obj_set_style_bg_color(r, cb_col(CB_SURFACE_HI), LV_STATE_PRESSED);
            lv_obj_add_event_cb(r, svc_click, LV_EVENT_CLICKED, (void *)(intptr_t)k_svc[i].goto_page);
        }
        icon_box(r, k_svc[i].tag, k_svc[i].color);
        lv_obj_t *mid = ui_col(r, 2);
        lv_obj_set_width(mid, LV_SIZE_CONTENT);
        lv_obj_set_flex_grow(mid, 1);
        ui_label(mid, k_svc[i].name, CB_F_VALUE, CB_TEXT_HI);
        O.svc_desc[i] = ui_label(mid, k_svc[i].desc, CB_F_LABEL, CB_TEXT_MID);
        ui_label(mid, k_svc[i].path, CB_F_MONO_S, CB_TEXT_LO);
        O.svc_state[i] = ui_label(r, "-", CB_F_BODY, CB_TEXT_MID);
    }

    lv_obj_t *conn = app_card(page, "SERVER CONNECTIVITY");
    lv_obj_t *r1 = ui_row(conn, 10);
    ui_label(r1, "Local base URL", CB_F_BODY, CB_TEXT_MID);
    ui_label(r1, net_base_url(), CB_F_MONO, CB_TEXT_HI);
    lv_obj_t *r2 = ui_row(conn, 10);
    ui_label(r2, "LAN base URL", CB_F_BODY, CB_TEXT_MID);
    char ip[48], u[96];
    lan_ip(ip, sizeof ip);
    snprintf(u, sizeof u, "https://%s:8443", ip);
    O.conn_lan = ui_label(r2, u, CB_F_MONO, CB_TEXT_HI);
    ui_spacer(r2);
    ui_chip(r2, "LAN", CB_GREEN, false);
    ui_label(conn, "OpenAI-shaped API. Bearer key required off-box; loopback is exempt.", CB_F_LABEL, CB_TEXT_LO);

    lv_obj_t *tt = ui_label(page, "Live telemetry", CB_F_TITLE, CB_TEXT_HI);
    lv_obj_set_style_pad_top(tt, 6, 0);
    lv_obj_t *tiles = app_flow(page, 14);
    static const char *const tl[N_TILES] = { "CPU Usage", "NPU Utilisation", "Memory Used", "Pi / NPU Temp" };
    for(int i = 0; i < N_TILES; i++) {
        app_tile(tiles, tl[i], &O.tv[i], &O.tb[i], 24);
        O.tsub[i] = ui_label(lv_obj_get_parent(O.tv[i]), "", CB_F_LABEL, CB_TEXT_LO);
    }

    lv_obj_t *ins = app_card(page, "RECENT REQUESTS");
    for(int i = 0; i < 4; i++) O.insp[i] = ui_label_line(ins, i == 0 ? "No requests yet" : "", CB_F_MONO_S, CB_TEXT_2);
}

static void bar_col(lv_obj_t *bar, double pct)
{
    lv_bar_set_value(bar, (int32_t)(pct + 0.5), LV_ANIM_OFF);
    lv_obj_set_style_bg_color(bar, cb_col(pct > 90 ? CB_RED : pct > 75 ? CB_ORANGE : CB_CYAN), LV_PART_INDICATOR);
}

static void tick(const net_snapshot_t *s)
{
    if(O.seen_seq == s->seq && O.seen_models == s->models_seq && O.seen_rows == s->rows_seq && s->seq != 0) return;
    O.seen_seq = s->seq;
    O.seen_models = s->models_seq;
    O.seen_rows = s->rows_seq;
    char b[96];

    int tot[N_SVC] = { 0 }, res[N_SVC] = { 0 }, failed[N_SVC] = { 0 }, nres = 0;
    for(int i = 0; i < s->n_models; i++) {
        int g = svc_of(s->models[i].modality);
        if(g < 0) continue;
        tot[g]++;
        if(s->models[i].resident) { res[g]++; nres++; }
        if(!strcmp(s->models[i].state, "failed")) failed[g]++;
    }
    snprintf(b, sizeof b, "%d / %d", nres, s->n_models);
    lv_label_set_text(O.cnt[0], s->models_ok ? b : "-");
    lv_label_set_text(O.cnt[1], "13");
    snprintf(b, sizeof b, "%d", s->clients);
    lv_label_set_text(O.cnt[2], b);
    snprintf(b, sizeof b, "%.1f", s->rps);
    lv_label_set_text(O.cnt[3], s->online ? b : "-");

    for(int i = 0; i < N_SVC; i++) {
        if(!s->models_ok) { lv_label_set_text(O.svc_state[i], "no data"); continue; }
        snprintf(b, sizeof b, failed[i] ? "%d failed" : "%d / %d loaded", failed[i] ? failed[i] : res[i], tot[i]);
        lv_label_set_text(O.svc_state[i], b);
        lv_obj_set_style_text_color(O.svc_state[i], cb_col(failed[i] ? CB_RED : res[i] == tot[i] ? CB_GREEN : CB_TEXT_2), 0);
    }

    if(s->seq) {
        bar_col(O.tb[0], s->cpu_pct);
        snprintf(b, sizeof b, "%.0f%%", s->cpu_pct);
        lv_label_set_text(O.tv[0], b);
        snprintf(b, sizeof b, "load %.2f  \xC2\xB7  %.0f MHz", s->load1, s->arm_mhz);
        lv_label_set_text(O.tsub[0], b);
        bar_col(O.tb[1], s->npu_util);
        snprintf(b, sizeof b, "%.0f%%", s->npu_util);
        lv_label_set_text(O.tv[1], b);
        snprintf(b, sizeof b, "GenAI: %s %s", s->genai_owner[0] ? s->genai_owner : "empty", s->genai_state);
        lv_label_set_text(O.tsub[1], b);
        double mp = s->mem_total > 0 ? 100.0 * s->mem_used / s->mem_total : 0;
        bar_col(O.tb[2], mp);
        snprintf(b, sizeof b, "%.1f GB", s->mem_used / 1024.0);
        lv_label_set_text(O.tv[2], b);
        snprintf(b, sizeof b, "of %.1f GB  \xC2\xB7  NPU RAM %.0f MB", s->mem_total / 1024.0, s->npu_ram_used);
        lv_label_set_text(O.tsub[2], b);
        bar_col(O.tb[3], s->temp_c);
        snprintf(b, sizeof b, "%.0f / %.0f C", s->temp_c, s->npu_temp);
        lv_label_set_text(O.tv[3], b);
        snprintf(b, sizeof b, "throttled %s", s->throttled);
        lv_label_set_text(O.tsub[3], b);
    }

    for(int i = 0; i < 4; i++) {
        if(i < s->n_rows) {
            const net_row_t *r = &s->rows[i];
            snprintf(b, sizeof b, "%s  %-26s %-14s %-4s q%.0f  i%.0f ms  %d", r->t, r->endpoint, r->model, r->device, r->queue_ms, r->infer_ms,
                     r->status);
            lv_label_set_text(O.insp[i], b);
        }
        else if(i > 0) lv_label_set_text(O.insp[i], "");
    }
}

const app_page_t page_overview = {
    .nav = "Overview", .icon = UI_ICON_GRID, .eyebrow = "EDGE AI / INFERENCE SERVER", .title = "Intelligence, at the edge.",
    .subtitle = "AI services, API endpoints, connected clients and live inference performance.",
    .build = build, .tick = tick,
};
