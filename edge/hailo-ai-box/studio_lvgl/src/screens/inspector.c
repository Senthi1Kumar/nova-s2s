#include "app.h"
#include <stdio.h>
#include <string.h>

static struct {
    lv_obj_t *table, *count;
    uint32_t seen_rows;
} I;

static void draw_cb(lv_event_t *e)
{
    lv_draw_task_t *task = lv_event_get_draw_task(e);
    lv_draw_dsc_base_t *base = lv_draw_task_get_draw_dsc(task);
    if(base->part != LV_PART_ITEMS) return;
    lv_draw_label_dsc_t *ld = lv_draw_task_get_label_dsc(task);
    if(!ld) return;
    uint32_t row = base->id1, col = base->id2;
    if(row == 0) {
        ld->color = cb_col(CB_TEXT_MID);
        return;
    }
    const char *v = lv_table_get_cell_value(lv_event_get_target_obj(e), row, col);
    if(col == 7) ld->color = cb_col(v[0] == '2' ? CB_GREEN : v[0] == '5' || v[0] == '4' ? CB_RED : CB_TEXT_2);
    else if(col == 3) ld->color = cb_col(CB_TEXT_HI);
}

static void build(lv_obj_t *page)
{
    lv_obj_t *card = app_card(page, "LAST 20 REQUESTS");
    I.count = ui_label(card, "", CB_F_LABEL, CB_TEXT_MID);
    I.table = lv_table_create(card);
    lv_obj_set_width(I.table, lv_pct(100));
    lv_obj_set_height(I.table, LV_SIZE_CONTENT);
    lv_obj_set_scrollable(I.table, false);
    lv_obj_set_style_bg_opa(I.table, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(I.table, 0, 0);
    lv_obj_set_style_pad_all(I.table, 0, 0);
    lv_obj_set_style_bg_opa(I.table, LV_OPA_TRANSP, LV_PART_ITEMS);
    lv_obj_set_style_border_width(I.table, 1, LV_PART_ITEMS);
    lv_obj_set_style_border_side(I.table, LV_BORDER_SIDE_BOTTOM, LV_PART_ITEMS);
    lv_obj_set_style_border_color(I.table, cb_col(CB_HAIRLINE), LV_PART_ITEMS);
    lv_obj_set_style_text_font(I.table, cb_font(CB_F_MONO), LV_PART_ITEMS);
    lv_obj_set_style_text_color(I.table, cb_col(CB_TEXT_2), LV_PART_ITEMS);
    lv_obj_set_style_pad_ver(I.table, 7, LV_PART_ITEMS);
    lv_obj_set_style_pad_hor(I.table, 8, LV_PART_ITEMS);
    int32_t w = app_content_w() - 34;
    static const int pct[8] = { 9, 14, 22, 18, 7, 10, 10, 10 };
    static const char *const hd[8] = { "Time", "Client", "Endpoint", "Model", "Device", "Queue", "Infer", "Status" };
    for(int i = 0; i < 8; i++) {
        lv_table_set_column_width(I.table, (uint32_t)i, w * pct[i] / 100);
        lv_table_set_cell_value(I.table, 0, (uint32_t)i, hd[i]);
    }
    app_table_fit(I.table, card, pct, 8);
    lv_table_set_row_count(I.table, 1);
    lv_obj_set_send_draw_task_events(I.table, true);
    lv_obj_add_event_cb(I.table, draw_cb, LV_EVENT_DRAW_TASK_ADDED, NULL);
}

static void tick(const net_snapshot_t *s)
{
    if(!I.table || I.seen_rows == s->rows_seq) return;
    I.seen_rows = s->rows_seq;
    char b[32];
    lv_table_set_row_count(I.table, (uint32_t)s->n_rows + 1);
    for(int i = 0; i < s->n_rows; i++) {
        const net_row_t *r = &s->rows[i];
        uint32_t row = (uint32_t)i + 1;
        lv_table_set_cell_value(I.table, row, 0, r->t);
        lv_table_set_cell_value(I.table, row, 1, r->client);
        lv_table_set_cell_value(I.table, row, 2, r->endpoint);
        lv_table_set_cell_value(I.table, row, 3, r->model);
        lv_table_set_cell_value(I.table, row, 4, r->device);
        snprintf(b, sizeof b, "%.0f ms", r->queue_ms);
        lv_table_set_cell_value(I.table, row, 5, b);
        snprintf(b, sizeof b, "%.0f ms", r->infer_ms);
        lv_table_set_cell_value(I.table, row, 6, b);
        snprintf(b, sizeof b, "%d", r->status);
        lv_table_set_cell_value(I.table, row, 7, b);
    }
    snprintf(b, sizeof b, "%d requests shown", s->n_rows);
    lv_label_set_text(I.count, b);
}

const app_page_t page_inspector = {
    .nav = "Inspector", .icon = UI_ICON_LIST, .eyebrow = "SYSTEM / REQUESTS", .title = "Request Inspector",
    .subtitle = "Every request the box served: client, endpoint, model, queue and inference time.",
    .build = build, .tick = tick,
};
