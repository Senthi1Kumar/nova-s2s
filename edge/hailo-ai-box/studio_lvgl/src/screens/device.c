#include "app.h"
#include "sparkline.h"
#include <stdio.h>
#include <string.h>

#define SPARKS 4
#define ROWS_MAX (NET_MAX_MODELS + 1)

static struct {
    lv_obj_t *arc[2], *arc_val[2], *arc_sub[2];
    lv_obj_t *mem_bar[2], *mem_lbl[2], *thr_lbl, *thr_sub, *own_lbl;
    lv_obj_t *spark[SPARKS], *spark_val[SPARKS];
    lv_obj_t *table, *events, *core_lbl;
    uint32_t seen_seq, seen_ev, seen_models;
} D;

static lv_obj_t *gauge_card(lv_obj_t *parent, const char *title, lv_obj_t **arc, lv_obj_t **val, lv_obj_t **sub)
{
    lv_obj_t *c = app_card(parent, title);
    lv_obj_set_width(c, lv_pct(23));
    lv_obj_set_flex_grow(c, 1);
    lv_obj_set_flex_align(c, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *a = lv_arc_create(c);
    lv_obj_set_size(a, 150, 150);
    lv_arc_set_rotation(a, 135);
    lv_arc_set_bg_angles(a, 0, 270);
    lv_arc_set_range(a, 0, 100);
    lv_obj_set_clickable(a, false);
    lv_obj_set_style_arc_width(a, 12, LV_PART_MAIN);
    lv_obj_set_style_arc_width(a, 12, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(a, cb_col(CB_HAIRLINE), LV_PART_MAIN);
    lv_obj_set_style_arc_color(a, cb_col(CB_CYAN), LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(a, true, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(a, LV_OPA_TRANSP, LV_PART_KNOB);
    lv_obj_set_style_pad_all(a, 0, LV_PART_KNOB);
    lv_obj_set_style_width(a, 0, LV_PART_KNOB);
    lv_obj_set_style_height(a, 0, LV_PART_KNOB);
    *arc = a;
    lv_obj_t *v = ui_label(a, "-", CB_F_TEMP, CB_TEXT_HI);
    lv_obj_center(v);
    *val = v;
    *sub = ui_label(c, "", CB_F_LABEL, CB_TEXT_MID);
    return c;
}

static lv_obj_t *spark_card(lv_obj_t *parent, const char *title, cb_color_t col, int32_t lo, int32_t hi, lv_obj_t **spark, lv_obj_t **val)
{
    lv_obj_t *c = app_card(parent, title);
    lv_obj_set_width(c, lv_pct(23));
    lv_obj_set_flex_grow(c, 1);
    *val = ui_label(c, "-", CB_F_TITLE, CB_TEXT_HI);
    *spark = sparkline_create(c, lv_pct(100), 64, 60, col, lo, hi);
    return c;
}

static void table_draw_cb(lv_event_t *e);

static void build(lv_obj_t *page)
{
    lv_obj_t *g = app_flow(page, 14);
    gauge_card(g, "CPU", &D.arc[0], &D.arc_val[0], &D.arc_sub[0]);
    gauge_card(g, "NPU (HAILO-10H)", &D.arc[1], &D.arc_val[1], &D.arc_sub[1]);
    lv_obj_t *m = app_card(g, "MEMORY");
    lv_obj_set_width(m, lv_pct(23));
    lv_obj_set_flex_grow(m, 1);
    for(int i = 0; i < 2; i++) {
        D.mem_lbl[i] = ui_label(m, "-", CB_F_BODY, CB_TEXT_2);
        D.mem_bar[i] = lv_bar_create(m);
        lv_obj_set_size(D.mem_bar[i], lv_pct(100), 8);
        lv_bar_set_range(D.mem_bar[i], 0, 100);
        lv_obj_set_style_bg_color(D.mem_bar[i], cb_col(CB_HAIRLINE), 0);
        lv_obj_set_style_bg_opa(D.mem_bar[i], LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(D.mem_bar[i], cb_col(i ? CB_VIOLET : CB_CYAN), LV_PART_INDICATOR);
        lv_obj_set_style_bg_opa(D.mem_bar[i], LV_OPA_COVER, LV_PART_INDICATOR);
        lv_obj_set_style_radius(D.mem_bar[i], 4, 0);
        lv_obj_set_style_radius(D.mem_bar[i], 4, LV_PART_INDICATOR);
    }
    D.core_lbl = ui_label(m, "", CB_F_MONO_S, CB_TEXT_MID);
    lv_obj_t *t = app_card(g, "GOVERNOR");
    lv_obj_set_width(t, lv_pct(23));
    lv_obj_set_flex_grow(t, 1);
    D.own_lbl = ui_label(t, "-", CB_F_VALUE, CB_TEXT_HI);
    D.thr_lbl = ui_label(t, "throttled -", CB_F_BODY, CB_TEXT_2);
    D.thr_sub = ui_label(t, "", CB_F_LABEL, CB_TEXT_MID);

    lv_obj_t *s = app_flow(page, 14);
    spark_card(s, "PI TEMPERATURE", CB_ORANGE, 40, 90, &D.spark[0], &D.spark_val[0]);
    spark_card(s, "NPU TEMPERATURE", CB_VIOLET, 30, 90, &D.spark[1], &D.spark_val[1]);
    spark_card(s, "ARM CLOCK", CB_GREEN, 500, 2800, &D.spark[2], &D.spark_val[2]);
    spark_card(s, "CPU LOAD", CB_CYAN, 0, 100, &D.spark[3], &D.spark_val[3]);

    lv_obj_t *low = app_flow(page, 14);
    lv_obj_t *res = app_card(low, "MODEL RESIDENCY");
    lv_obj_set_width(res, lv_pct(60));
    lv_obj_set_flex_grow(res, 3);
    D.table = lv_table_create(res);
    lv_obj_set_width(D.table, lv_pct(100));
    lv_obj_set_height(D.table, LV_SIZE_CONTENT);
    lv_obj_set_scrollable(D.table, false);
    lv_obj_set_style_bg_opa(D.table, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(D.table, 0, 0);
    lv_obj_set_style_pad_all(D.table, 0, 0);
    lv_obj_set_style_bg_opa(D.table, LV_OPA_TRANSP, LV_PART_ITEMS);
    lv_obj_set_style_border_width(D.table, 1, LV_PART_ITEMS);
    lv_obj_set_style_border_side(D.table, LV_BORDER_SIDE_BOTTOM, LV_PART_ITEMS);
    lv_obj_set_style_border_color(D.table, cb_col(CB_HAIRLINE), LV_PART_ITEMS);
    lv_obj_set_style_text_font(D.table, cb_font(CB_F_BODY), LV_PART_ITEMS);
    lv_obj_set_style_text_color(D.table, cb_col(CB_TEXT_2), LV_PART_ITEMS);
    lv_obj_set_style_pad_ver(D.table, 6, LV_PART_ITEMS);
    lv_obj_set_style_pad_hor(D.table, 6, LV_PART_ITEMS);
    int32_t w = app_content_w() * 60 / 100 - 36;
    static const int pct[6] = { 26, 16, 12, 14, 14, 18 };
    static const char *const hd[6] = { "Model", "State", "FPS", "p50", "p95", "Requests" };
    for(int i = 0; i < 6; i++) {
        lv_table_set_column_width(D.table, (uint32_t)i, w * pct[i] / 100);
        lv_table_set_cell_value(D.table, 0, (uint32_t)i, hd[i]);
    }
    app_table_fit(D.table, res, pct, 6);
    lv_table_set_row_count(D.table, 1);
    lv_obj_add_event_cb(D.table, table_draw_cb, LV_EVENT_DRAW_TASK_ADDED, NULL);
    lv_obj_set_send_draw_task_events(D.table, true);

    lv_obj_t *ev = app_card(low, "GOVERNOR EVENTS");
    lv_obj_set_flex_grow(ev, 2);
    lv_obj_set_width(ev, lv_pct(36));
    D.events = ui_label(ev, "No events yet", CB_F_MONO_S, CB_TEXT_2);
    lv_obj_set_width(D.events, lv_pct(100));
    lv_label_set_long_mode(D.events, LV_LABEL_LONG_MODE_DOTS);
}

static void set_arc(int i, double pct, const char *sub)
{
    char b[24];
    lv_arc_set_value(D.arc[i], (int32_t)(pct + 0.5));
    lv_obj_set_style_arc_color(D.arc[i], cb_col(pct > 90 ? CB_RED : pct > 75 ? CB_ORANGE : CB_CYAN), LV_PART_INDICATOR);
    snprintf(b, sizeof b, "%.0f%%", pct);
    lv_label_set_text(D.arc_val[i], b);
    lv_label_set_text(D.arc_sub[i], sub);
}

static void table_draw_cb(lv_event_t *e)
{
    lv_draw_task_t *task = lv_event_get_draw_task(e);
    lv_draw_dsc_base_t *base = lv_draw_task_get_draw_dsc(task);
    if(base->part != LV_PART_ITEMS) return;
    uint32_t row = base->id1, col = base->id2;
    lv_draw_label_dsc_t *ld = lv_draw_task_get_label_dsc(task);
    if(!ld) return;
    lv_obj_t *t = lv_event_get_target_obj(e);
    if(row == 0) {
        ld->color = cb_col(CB_TEXT_MID);
        return;
    }
    if(col == 1) {
        const char *st = lv_table_get_cell_value(t, row, 1);
        ld->color = cb_col(!strcmp(st, "resident") ? CB_GREEN : !strcmp(st, "failed") ? CB_RED : !strcmp(st, "loading") ? CB_YELLOW : CB_TEXT_MID);
    }
    else if(col == 0) ld->color = cb_col(CB_TEXT_HI);
}

static void tick(const net_snapshot_t *s)
{
    if(!D.table) return;
    char b[160];
    if(s->seq && D.seen_seq != s->seq) {
        D.seen_seq = s->seq;
        snprintf(b, sizeof b, "%d cores  \xC2\xB7  load %.2f", s->ncores, s->load1);
        set_arc(0, s->cpu_pct, b);
        snprintf(b, sizeof b, "NNC util  \xC2\xB7  %.0f C", s->npu_temp);
        set_arc(1, s->npu_util, b);
        double mp = s->mem_total > 0 ? 100.0 * s->mem_used / s->mem_total : 0;
        snprintf(b, sizeof b, "Pi RAM  %.0f / %.0f MB", s->mem_used, s->mem_total);
        lv_label_set_text(D.mem_lbl[0], b);
        lv_bar_set_value(D.mem_bar[0], (int32_t)mp, LV_ANIM_OFF);
        double np = s->npu_ram_total > 0 ? 100.0 * s->npu_ram_used / s->npu_ram_total : 0;
        snprintf(b, sizeof b, "NPU RAM  %.0f / %.0f MB", s->npu_ram_used, s->npu_ram_total);
        lv_label_set_text(D.mem_lbl[1], b);
        lv_bar_set_value(D.mem_bar[1], (int32_t)np, LV_ANIM_OFF);
        char cores[96] = "";
        for(int i = 0; i < s->ncores; i++) snprintf(cores + strlen(cores), sizeof cores - strlen(cores), "%s%.0f", i ? " " : "cores ", s->core[i]);
        lv_label_set_text(D.core_lbl, cores);
        snprintf(b, sizeof b, "GenAI: %s", s->genai_owner[0] ? s->genai_owner : "empty");
        lv_label_set_text(D.own_lbl, b);
        snprintf(b, sizeof b, "state %s  \xC2\xB7  throttled %s", s->genai_state[0] ? s->genai_state : "-", s->throttled);
        lv_label_set_text(D.thr_lbl, b);
        lv_label_set_text(D.thr_sub, strcmp(s->throttled, "0x0") ? "Pi is or was throttled" : "Pi never throttled this boot");

        snprintf(b, sizeof b, "%.1f C", s->temp_c);
        lv_label_set_text(D.spark_val[0], b);
        sparkline_push(D.spark[0], (int32_t)s->temp_c);
        snprintf(b, sizeof b, "%.1f C", s->npu_temp);
        lv_label_set_text(D.spark_val[1], b);
        sparkline_push(D.spark[1], (int32_t)s->npu_temp);
        snprintf(b, sizeof b, "%.0f MHz", s->arm_mhz);
        lv_label_set_text(D.spark_val[2], b);
        sparkline_push(D.spark[2], (int32_t)s->arm_mhz);
        snprintf(b, sizeof b, "%.0f%%", s->cpu_pct);
        lv_label_set_text(D.spark_val[3], b);
        sparkline_push(D.spark[3], (int32_t)s->cpu_pct);

        /* merge residency (models list) with live stats (stream), by id */
        int n = s->n_models;
        lv_table_set_row_count(D.table, (uint32_t)(n + 1));
        for(int i = 0; i < n; i++) {
            const net_model_t *m = &s->models[i];
            const net_mstat_t *st = NULL;
            for(int k = 0; k < s->n_ms; k++)
                if(!strcmp(s->ms[k].id, m->id)) st = &s->ms[k];
            uint32_t r = (uint32_t)i + 1;
            lv_table_set_cell_value(D.table, r, 0, m->id);
            lv_table_set_cell_value(D.table, r, 1, st ? st->state : m->state);
            if(st) {
                snprintf(b, sizeof b, "%.1f", st->fps);
                lv_table_set_cell_value(D.table, r, 2, b);
                snprintf(b, sizeof b, "%.0f ms", st->p50);
                lv_table_set_cell_value(D.table, r, 3, b);
                snprintf(b, sizeof b, "%.0f ms", st->p95);
                lv_table_set_cell_value(D.table, r, 4, b);
                snprintf(b, sizeof b, "%ld", st->requests);
                lv_table_set_cell_value(D.table, r, 5, b);
            }
            else for(uint32_t c = 2; c < 6; c++) lv_table_set_cell_value(D.table, r, c, "-");
        }
    }
    if(D.seen_ev != s->ev_seq) {
        D.seen_ev = s->ev_seq;
        char ev[NET_MAX_EVENTS * 124];
        ev[0] = '\0';
        for(int i = 0; i < s->n_events && i < 14; i++) {
            strncat(ev, s->events[i], sizeof ev - strlen(ev) - 2);
            strcat(ev, "\n");
        }
        lv_label_set_text(D.events, ev[0] ? ev : "No events yet");
    }
}

const app_page_t page_device = {
    .nav = "Device Status", .icon = UI_ICON_GEAR, .eyebrow = "SYSTEM / HARDWARE", .title = "Device Status",
    .subtitle = "Raspberry Pi 5 host, Hailo-10H NPU, model residency and governor events.",
    .build = build, .tick = tick,
};
