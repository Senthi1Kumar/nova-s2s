#include "sparkline.h"

#define FILL_OPA_MAX 60   /* opacity of the fill right under the line; fades to 0 at the bottom */

static void set_grad(lv_grad_dsc_t *g, lv_color_t c, lv_opa_t top, lv_opa_t bottom)
{
    g->dir = LV_GRAD_DIR_VER;
    g->stops_count = 2;
    g->stops[0].color = c;
    g->stops[0].opa = top;
    g->stops[0].frac = 0;
    g->stops[1].color = c;
    g->stops[1].opa = bottom;
    g->stops[1].frac = 255;
}

/* For every line segment the chart draws, add a triangle (under the slope) and a rectangle (down to the
   bottom edge) filled with a vertical gradient: LVGL's lv_example_chart_area_gradient technique. */
static void fill_under_line(lv_event_t *e)
{
    lv_draw_task_t *task = lv_event_get_draw_task(e);
    lv_draw_dsc_base_t *base = lv_draw_task_get_draw_dsc(task);
    if(base->part != LV_PART_ITEMS || lv_draw_task_get_type(task) != LV_DRAW_TASK_TYPE_LINE) return;

    lv_obj_t *chart = lv_event_get_target_obj(e);
    lv_area_t coords;
    lv_obj_get_coords(chart, &coords);
    int32_t full_h = lv_area_get_height(&coords);
    if(full_h <= 0) return;
    lv_color_t c = lv_chart_get_series_color(chart, lv_chart_get_series_next(chart, NULL));
    lv_draw_line_dsc_t *line = lv_draw_task_get_line_dsc(task);

    for(int32_t i = 0; i + 1 < line->point_cnt; i++) {
        lv_point_precise_t p1 = line->points[i];
        lv_point_precise_t p2 = line->points[i + 1];
        if(p1.x == LV_DRAW_LINE_POINT_NONE || p1.y == LV_DRAW_LINE_POINT_NONE ||
           p2.x == LV_DRAW_LINE_POINT_NONE || p2.y == LV_DRAW_LINE_POINT_NONE) continue;
        int32_t top = (int32_t)LV_MIN(p1.y, p2.y);
        int32_t bottom = (int32_t)LV_MAX(p1.y, p2.y);
        lv_opa_t opa_top = (lv_opa_t)(FILL_OPA_MAX * (coords.y2 - top) / full_h);
        lv_opa_t opa_bottom = (lv_opa_t)(FILL_OPA_MAX * (coords.y2 - bottom) / full_h);

        lv_draw_triangle_dsc_t tri;
        lv_draw_triangle_dsc_init(&tri);
        tri.p[0] = p1;
        tri.p[1] = p2;
        tri.p[2].x = p1.y < p2.y ? p1.x : p2.x;
        tri.p[2].y = bottom;
        set_grad(&tri.grad, c, opa_top, opa_bottom);
        lv_draw_triangle(base->layer, &tri);

        lv_draw_rect_dsc_t rect;
        lv_draw_rect_dsc_init(&rect);
        set_grad(&rect.bg_grad, c, opa_bottom, 0);
        lv_area_t area = { (int32_t)p1.x, bottom, (int32_t)p2.x - 1, coords.y2 };
        lv_draw_rect(base->layer, &rect, &area);
    }
}

lv_obj_t *sparkline_create(lv_obj_t *parent, int32_t w, int32_t h, uint32_t points, cb_color_t color,
                           int32_t min, int32_t max)
{
    lv_obj_t *chart = lv_chart_create(parent);
    lv_obj_remove_style_all(chart);
    lv_obj_set_size(chart, w, h);
    lv_obj_set_clickable(chart, false);
    lv_obj_set_scrollable(chart, false);
    lv_chart_set_type(chart, LV_CHART_TYPE_LINE);
    lv_chart_set_point_count(chart, points);
    lv_chart_set_div_line_count(chart, 0, 0);
    lv_chart_set_axis_range(chart, LV_CHART_AXIS_PRIMARY_Y, min, max);
    lv_chart_set_update_mode(chart, LV_CHART_UPDATE_MODE_SHIFT);
    lv_obj_set_style_line_width(chart, 2, LV_PART_ITEMS);
    lv_obj_set_style_line_rounded(chart, true, LV_PART_ITEMS);
    lv_obj_set_style_width(chart, 0, LV_PART_INDICATOR);    /* no point markers */
    lv_obj_set_style_height(chart, 0, LV_PART_INDICATOR);
    lv_obj_set_send_draw_task_events(chart, true);
    lv_obj_add_event_cb(chart, fill_under_line, LV_EVENT_DRAW_TASK_ADDED, NULL);
    lv_chart_series_t *ser = lv_chart_add_series(chart, cb_col(color), LV_CHART_AXIS_PRIMARY_Y);
    lv_chart_set_all_values(chart, ser, (min + max) / 2);
    return chart;
}

void sparkline_push(lv_obj_t *spark, int32_t value)
{
    lv_chart_set_next_value(spark, lv_chart_get_series_next(spark, NULL), value);
}
