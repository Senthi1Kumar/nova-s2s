#include "app.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Computer Vision: WS /v1/vision/stream with rgb565 previews. The net thread keeps the newest frame in a triple
   buffer; this page blits it into an lv_canvas (no copy: the canvas points at the front buffer) and draws the
   detections and up to four class-colored segmentation masks as LVGL objects on top. */

#define KP_POOL 96
#define PCOUNT 3

static const char *const k_task_id[6] = { "detection", "pose", "segmentation", "lanes", "dms", "ocr" };
static const char *const k_task_name = "Object Detection\nPose Estimation\nSegmentation\nLane Detection\nDriver Monitoring\nOCR";
static const char *const k_src_id[2] = { "c920e", "f455" };
static const char *const k_src_name = "C920e (USB webcam)\nF455 (depth camera)";
#define N_CAM 2
static const cb_color_t k_pal[5] = { CB_CYAN, CB_GREEN, CB_ORANGE, CB_VIOLET, CB_YELLOW };

static struct {
    lv_obj_t *stage, *view, *canvas, *wait, *dd_task, *dd_src, *btn, *btn_lbl;
    lv_obj_t *box[NET_MAX_BOXES], *box_lbl[NET_MAX_BOXES], *kp[KP_POOL], *lane[NET_MAX_LANES];
    lv_obj_t *mask_cv[NET_MAX_MASKS];
    lv_draw_buf_t mask_db[NET_MAX_MASKS];
    uint32_t *mask_mem[NET_MAX_MASKS];
    size_t mask_cap[NET_MAX_MASKS];
    lv_point_precise_t lane_pts[NET_MAX_LANES][NET_LANE_PTS];
    lv_obj_t *dms_chip, *dms_chip_lbl, *dms_sum, *d_vt;
    char vid_id[NET_MAX_VIDEOS][40], vid_name[NET_MAX_VIDEOS][40];
    int n_vid;
    uint32_t seen_vseq;
    lv_obj_t *d_task, *d_src, *d_res, *d_fps, *d_pfps, *d_lat, *d_det, *d_state, *d_info;
    lv_timer_t *timer;
    const char *demo_task, *demo_src;
    lv_draw_buf_t db[PCOUNT];
    const uint16_t *db_pix[PCOUNT];
    int32_t max_w, max_h;
    bool running;
    uint32_t seen_p, seen_r, fps_t0, fps_n, label_t;
    double pfps;
    int cur_w, cur_h;
    float scale;
    net_vision_t v;
} V;

/* Neutral placeholder: no stale frame, mask, box, keypoint or lane stays on screen. */
static void clear_view(const char *msg)
{
    lv_obj_set_hidden(V.view, true);
    for(int i = 0; i < NET_MAX_MASKS; i++) lv_obj_set_hidden(V.mask_cv[i], true);
    for(int i = 0; i < NET_MAX_BOXES; i++) {
        lv_obj_set_hidden(V.box[i], true);
        lv_obj_set_hidden(V.box_lbl[i], true);
    }
    for(int i = 0; i < KP_POOL; i++) lv_obj_set_hidden(V.kp[i], true);
    for(int i = 0; i < NET_MAX_LANES; i++) lv_obj_set_hidden(V.lane[i], true);
    V.cur_w = V.cur_h = 0;
    lv_label_set_text(V.wait, msg);
    lv_obj_set_hidden(V.wait, false);
    lv_obj_invalidate(V.stage);
}

static void src_string(char *out, size_t cap)
{
    uint32_t i = lv_dropdown_get_selected(V.dd_src);
    if(i < N_CAM) snprintf(out, cap, "%s", k_src_id[i]);
    else if(i - N_CAM < (uint32_t)V.n_vid) snprintf(out, cap, "video:%s", V.vid_id[i - N_CAM]);
    else snprintf(out, cap, "%s", k_src_id[0]);
}

static void start_stream(void)
{
    char src[48];
    src_string(src, sizeof src);
    clear_view(!strncmp(src, "video:", 6) ? "Starting video..." : "Starting camera...");
    net_vision_start(src, k_task_id[lv_dropdown_get_selected(V.dd_task)], 20, 480);
    V.running = true;
    net_vision_take(&V.v);   /* frames and results already buffered belong to the previous task or source */
    V.seen_p = V.v.pgen;
    V.seen_r = V.v.rgen;
    lv_label_set_text(V.btn_lbl, "Stop");
}

static void stop_stream(void)
{
    net_vision_stop();
    V.running = false;
    lv_label_set_text(V.btn_lbl, "Start");
    clear_view("Stopped \xE2\x80\x94 press Start");
}

static void dd_cb(lv_event_t *e)
{
    /* Lane Detection is meant for road footage: switch to the first video preset when there is one. */
    if(lv_event_get_target(e) == V.dd_task && !strcmp(k_task_id[lv_dropdown_get_selected(V.dd_task)], "lanes") &&
       lv_dropdown_get_selected(V.dd_src) < N_CAM && V.n_vid > 0)
        lv_dropdown_set_selected(V.dd_src, N_CAM);
    if(V.running) start_stream();
}

static void btn_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if(V.running) stop_stream();
    else start_stream();
}

static lv_obj_t *dropdown(lv_obj_t *parent, const char *opts, int32_t w)
{
    lv_obj_t *d = lv_dropdown_create(parent);
    lv_dropdown_set_options(d, opts);
    lv_obj_set_width(d, w);
    lv_obj_set_style_text_font(d, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_bg_color(d, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(d, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_color(d, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(d, 1, 0);
    lv_obj_set_style_radius(d, 8, 0);
    lv_obj_set_style_text_font(d, &lv_font_montserrat_14, LV_PART_INDICATOR);
    lv_obj_set_style_text_color(d, cb_col(CB_TEXT_MID), LV_PART_INDICATOR);
    lv_obj_t *list = lv_dropdown_get_list(d);
    lv_obj_set_style_bg_color(list, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(list, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(list, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(list, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_border_color(list, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_bg_color(list, cb_col(CB_CYAN), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(list, cb_col(CB_BASE), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_add_event_cb(d, dd_cb, LV_EVENT_VALUE_CHANGED, NULL);
    return d;
}

static lv_obj_t *kv(lv_obj_t *parent, const char *key)
{
    lv_obj_t *r = ui_row(parent, 8);
    ui_label(r, key, CB_F_BODY, CB_TEXT_MID);
    ui_spacer(r);
    return ui_label(r, "-", CB_F_BODY, CB_TEXT_HI);
}

static lv_obj_t *overlay_box(lv_obj_t *view, int i, lv_obj_t **lbl)
{
    lv_obj_t *b = ui_plain(view);
    lv_obj_set_style_border_color(b, cb_col(k_pal[i % 5]), 0);
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_radius(b, 3, 0);
    lv_obj_set_style_clip_corner(b, false, 0);
    lv_obj_set_hidden(b, true);
    lv_obj_t *l = ui_label(view, "", CB_F_LABEL, CB_BASE);
    lv_obj_set_hidden(l, true);
    lv_obj_set_style_bg_color(l, cb_col(k_pal[i % 5]), 0);
    lv_obj_set_style_bg_opa(l, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(l, 4, 0);
    lv_obj_set_style_radius(l, 3, 0);
    lv_obj_set_style_text_font(l, cb_font(CB_F_LABEL), 0);
    lv_obj_set_scrollable(b, false);
    lv_obj_set_overflow_visible(b, true);
    *lbl = l;
    return b;
}

static void build(lv_obj_t *page)
{
    lv_obj_set_flex_flow(page, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(page, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(page, 14, 0);
    lv_obj_set_scroll_dir(page, LV_DIR_VER);

    int32_t side_w = 400;

    lv_obj_t *left = app_card(page, NULL);
    lv_obj_set_width(left, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(left, 1);
    lv_obj_t *ctl = ui_row(left, 10);
    lv_obj_set_flex_flow(ctl, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_height(ctl, LV_SIZE_CONTENT);
    ui_label(ctl, "Task", CB_F_BODY, CB_TEXT_MID);
    V.dd_task = dropdown(ctl, k_task_name, 210);
    ui_label(ctl, "Source", CB_F_BODY, CB_TEXT_MID);
    V.dd_src = dropdown(ctl, k_src_name, 230);
    ui_spacer(ctl);
    V.btn = lv_button_create(ctl);
    lv_obj_set_size(V.btn, 110, 38);
    lv_obj_set_style_bg_color(V.btn, cb_col(CB_CYAN), 0);
    lv_obj_set_style_bg_opa(V.btn, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(V.btn, 8, 0);
    lv_obj_set_style_shadow_width(V.btn, 0, 0);
    V.btn_lbl = ui_label(V.btn, "Stop", CB_F_VALUE, CB_BASE);
    lv_obj_center(V.btn_lbl);
    lv_obj_add_event_cb(V.btn, btn_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t *stage = ui_plain(left);
    V.stage = stage;
    lv_obj_set_size(stage, lv_pct(100), 600);
    lv_obj_set_style_bg_color(stage, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(stage, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(stage, 10, 0);
    lv_obj_set_flex_flow(stage, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(stage, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    V.view = ui_plain(stage);
    lv_obj_set_size(V.view, 480, 270);
    lv_obj_set_overflow_visible(V.view, false);
    V.canvas = lv_canvas_create(V.view);
    lv_obj_set_pos(V.canvas, 0, 0);
    lv_obj_set_style_bg_opa(V.canvas, LV_OPA_TRANSP, 0);
    for(int i = 0; i < NET_MAX_MASKS; i++) {
        V.mask_cv[i] = lv_canvas_create(V.view);
        lv_obj_set_pos(V.mask_cv[i], 0, 0);
        lv_obj_set_hidden(V.mask_cv[i], true);
    }
    for(int i = 0; i < NET_MAX_BOXES; i++) V.box[i] = overlay_box(V.view, i, &V.box_lbl[i]);
    for(int i = 0; i < NET_MAX_LANES; i++) {
        V.lane[i] = lv_line_create(V.view);
        lv_obj_set_style_line_color(V.lane[i], cb_col(i % 2 ? CB_YELLOW : CB_GREEN), 0);
        lv_obj_set_style_line_width(V.lane[i], 4, 0);
        lv_obj_set_style_line_rounded(V.lane[i], true, 0);
        lv_obj_set_hidden(V.lane[i], true);
    }
    for(int i = 0; i < KP_POOL; i++) {
        V.kp[i] = ui_plain(V.view);
        lv_obj_set_size(V.kp[i], 8, 8);
        lv_obj_set_style_radius(V.kp[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(V.kp[i], cb_col(CB_YELLOW), 0);
        lv_obj_set_style_bg_opa(V.kp[i], LV_OPA_COVER, 0);
        lv_obj_set_hidden(V.kp[i], true);
    }
    V.wait = ui_label(stage, "Starting camera...", CB_F_VALUE, CB_TEXT_MID);
    lv_obj_set_hidden(V.wait, false);
    lv_obj_set_style_text_align(V.wait, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_floating(V.wait, true);
    lv_obj_align(V.wait, LV_ALIGN_CENTER, 0, 0);

    lv_obj_t *side = app_card(page, "INFERENCE DIAGNOSTICS");
    lv_obj_set_width(side, side_w);
    V.dms_chip = ui_plain(side);
    lv_obj_set_size(V.dms_chip, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(V.dms_chip, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(V.dms_chip, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(V.dms_chip, 16, 0);
    lv_obj_set_style_pad_ver(V.dms_chip, 6, 0);
    V.dms_chip_lbl = ui_label(V.dms_chip, "", CB_F_VALUE, CB_BASE);
    V.dms_sum = ui_label(side, "", CB_F_TITLE, CB_TEXT_HI);
    lv_obj_set_width(V.dms_sum, lv_pct(100));
    lv_label_set_long_mode(V.dms_sum, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_hidden(V.dms_chip, true);
    lv_obj_set_hidden(V.dms_sum, true);
    V.d_task = kv(side, "Task");
    V.d_src = kv(side, "Source");
    V.d_res = kv(side, "Resolution");
    lv_label_set_text(kv(side, "Engine"), "Hailo-10H");
    V.d_fps = kv(side, "Server FPS");
    V.d_pfps = kv(side, "Preview FPS");
    V.d_lat = kv(side, "Inference latency");
    V.d_det = kv(side, "Detections");
    V.d_vt = kv(side, "Video time");
    V.d_state = ui_label(side, "", CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_width(V.d_state, lv_pct(100));
    lv_label_set_long_mode(V.d_state, LV_LABEL_LONG_MODE_WRAP);
    V.d_info = ui_label(side, "", CB_F_LABEL, CB_TEXT_MID);
    lv_obj_set_width(V.d_info, lv_pct(100));
    lv_label_set_long_mode(V.d_info, LV_LABEL_LONG_MODE_WRAP);
}

/* The stage is sized from the real display (the window starts small and goes fullscreen later), so refit
   whenever its size or the frame size changes. */
static void fit_view(int w, int h)
{
    int32_t sh_px = lv_display_get_vertical_resolution(NULL) - 340;
    if(sh_px < 300) sh_px = 300;
    if(lv_obj_get_height(V.stage) != sh_px) lv_obj_set_height(V.stage, sh_px);
    lv_obj_update_layout(V.stage);
    int32_t mw = lv_obj_get_content_width(V.stage), mh = lv_obj_get_content_height(V.stage);
    if(w == V.cur_w && h == V.cur_h && mw == V.max_w && mh == V.max_h) return;
    V.cur_w = w;
    V.cur_h = h;
    V.max_w = mw;
    V.max_h = mh;
    float s = (float)V.max_w / (float)w;
    float sh = (float)V.max_h / (float)h;
    if(sh < s) s = sh;
    if(s > 3.0f) s = 3.0f;
    V.scale = s;
    lv_obj_set_size(V.view, (int32_t)(w * s), (int32_t)(h * s));
    lv_image_set_pivot(V.canvas, 0, 0);
    lv_image_set_scale(V.canvas, (uint32_t)(256.0f * s));
}

static void show_frame(const net_vision_t *v)
{
    int k = -1;
    for(int i = 0; i < PCOUNT; i++)
        if(V.db_pix[i] == v->pix) k = i;
    if(k < 0)
        for(int i = 0; i < PCOUNT && k < 0; i++)
            if(!V.db_pix[i]) { k = i; V.db_pix[i] = v->pix; }
    if(k < 0) k = 0;
    fit_view(v->w, v->h);
    lv_draw_buf_init(&V.db[k], (uint32_t)v->w, (uint32_t)v->h, LV_COLOR_FORMAT_RGB565, (uint32_t)v->w * 2, (void *)v->pix, (uint32_t)(v->w * v->h * 2));
    lv_canvas_set_draw_buf(V.canvas, &V.db[k]);
    lv_obj_invalidate(V.canvas);
    lv_obj_set_hidden(V.view, false);
    lv_obj_set_hidden(V.wait, true);
    V.fps_n++;
}

static void render_masks(const net_vision_t *v)
{
    for(int i = 0; i < NET_MAX_MASKS; i++) {
        const net_mask_t *m = i < v->n_masks ? &v->masks[i] : NULL;
        if(!m || m->w <= 0 || m->h <= 0 || v->w <= 0 || v->h <= 0) {
            lv_obj_set_hidden(V.mask_cv[i], true);
            continue;
        }
        size_t pixels = (size_t)m->w * (size_t)m->h;
        size_t needed = (size_t)v->w * (size_t)v->h;
        if(V.mask_cap[i] < needed) {
            uint32_t *next = realloc(V.mask_mem[i], needed * sizeof *next);
            if(!next) {
                lv_obj_set_hidden(V.mask_cv[i], true);
                continue;
            }
            V.mask_mem[i] = next;
            V.mask_cap[i] = needed;
        }
        uint8_t *bits = malloc(pixels);
        if(!bits) {
            lv_obj_set_hidden(V.mask_cv[i], true);
            continue;
        }
        memset(bits, 0, pixels);
        size_t pos = 0;
        int value = 0;
        bool valid = true;
        for(int k = 0; k < m->n_runs; k++) {
            size_t run = m->counts[k];
            if(run > pixels - pos) { valid = false; break; }
            if(value) memset(bits + pos, 1, run);
            pos += run;
            value ^= 1;
        }
        if(!valid || pos != pixels) {
            free(bits);
            lv_obj_set_hidden(V.mask_cv[i], true);
            continue;
        }
        lv_color_t c = cb_col(k_pal[m->box % 5]);
        uint32_t argb = (96u << 24) | ((uint32_t)c.red << 16) | ((uint32_t)c.green << 8) | c.blue;
        for(int y = 0; y < v->h; y++) {
            int sy = y * m->h / v->h;
            for(int x = 0; x < v->w; x++) {
                int sx = x * m->w / v->w;
                V.mask_mem[i][y * v->w + x] = bits[sx * m->h + sy] ? argb : 0;
            }
        }
        free(bits);
        lv_draw_buf_init(&V.mask_db[i], (uint32_t)v->w, (uint32_t)v->h, LV_COLOR_FORMAT_ARGB8888,
                         (uint32_t)v->w * 4, V.mask_mem[i], needed * 4);
        lv_canvas_set_draw_buf(V.mask_cv[i], &V.mask_db[i]);
        lv_image_set_pivot(V.mask_cv[i], 0, 0);
        lv_image_set_scale(V.mask_cv[i], (uint32_t)(256.0f * V.scale));
        lv_obj_set_hidden(V.mask_cv[i], false);
    }
}

static void show_overlays(const net_vision_t *v)
{
    render_masks(v);
    float sx = V.scale, sy = V.scale;
    if(v->img_w > 0 && V.cur_w > 0) sx = V.scale * (float)V.cur_w / (float)v->img_w;
    if(v->img_h > 0 && V.cur_h > 0) sy = V.scale * (float)V.cur_h / (float)v->img_h;
    for(int i = 0; i < NET_MAX_BOXES; i++) {
        bool on = i < v->n_boxes;
        lv_obj_set_hidden(V.box[i], !on);
        lv_obj_set_hidden(V.box_lbl[i], !on || !v->boxes[i].label[0]);
        if(!on) continue;
        const net_box_t *b = &v->boxes[i];
        lv_obj_set_pos(V.box[i], (int32_t)(b->x1 * sx), (int32_t)(b->y1 * sy));
        lv_obj_set_size(V.box[i], (int32_t)((b->x2 - b->x1) * sx) + 1, (int32_t)((b->y2 - b->y1) * sy) + 1);
        lv_label_set_text(V.box_lbl[i], b->label);
        int32_t ly = (int32_t)(b->y1 * sy) - 22;
        lv_obj_set_pos(V.box_lbl[i], (int32_t)(b->x1 * sx), ly < 0 ? (int32_t)(b->y1 * sy) + 2 : ly);
    }
    for(int i = 0; i < KP_POOL; i++) {
        bool on = i < v->n_kp;
        lv_obj_set_hidden(V.kp[i], !on);
        if(on) lv_obj_set_pos(V.kp[i], (int32_t)(v->kp[i][0] * sx) - 4, (int32_t)(v->kp[i][1] * sy) - 4);
    }
    for(int i = 0; i < NET_MAX_LANES; i++) {
        bool on = i < v->n_lanes && v->lanes[i].n >= 2;
        lv_obj_set_hidden(V.lane[i], !on);
        if(!on) continue;
        for(int k = 0; k < v->lanes[i].n; k++) {
            V.lane_pts[i][k].x = v->lanes[i].x[k] * sx;
            V.lane_pts[i][k].y = v->lanes[i].y[k] * sy;
        }
        lv_line_set_points(V.lane[i], V.lane_pts[i], (uint32_t)v->lanes[i].n);
    }
}

static void update_labels(const net_vision_t *v_in)
{
    static const net_vision_t k_none;
    const net_vision_t *v = V.running ? v_in : &k_none;
    char b[96];
    int ti = lv_dropdown_get_selected(V.dd_task), si = (int)lv_dropdown_get_selected(V.dd_src);
    bool is_video = si >= N_CAM;
    lv_label_set_text(V.d_task, k_task_id[ti]);
    {
        char sb[48];
        src_string(sb, sizeof sb);
        lv_label_set_text(V.d_src, !strncmp(sb, "video:", 6) ? sb + 6 : sb);
    }
    if(v->video_t >= 0) snprintf(b, sizeof b, "%.1f s", v->video_t);
    else snprintf(b, sizeof b, "-");
    lv_label_set_text(V.d_vt, b);
    if(v->w) {
        snprintf(b, sizeof b, "%dx%d preview", v->w, v->h);
        lv_label_set_text(V.d_res, b);
    }
    snprintf(b, sizeof b, "%.1f", v->fps);
    lv_label_set_text(V.d_fps, b);
    snprintf(b, sizeof b, "%.1f", V.pfps);
    lv_label_set_text(V.d_pfps, b);
    app_fmt_ms(b, sizeof b, v->infer_ms);
    lv_label_set_text(V.d_lat, b);
    snprintf(b, sizeof b, "%d", v->n_det);
    lv_label_set_text(V.d_det, b);
    char st[160];
    if(!V.running) snprintf(st, sizeof st, "Stopped.");
    else if(v->loading[0]) snprintf(st, sizeof st, "Loading %s\xE2\x80\xA6", v->loading);
    else if(v->err[0]) snprintf(st, sizeof st, "%s", v->err);
    else if(v->degraded[0]) snprintf(st, sizeof st, "Degraded: %s", v->degraded);
    else if(!v->open) snprintf(st, sizeof st, "Connecting to the stream...");
    else if(is_video) snprintf(st, sizeof st, "Playing the video.");
    else snprintf(st, sizeof st, "Streaming from the %s camera.", k_src_id[si]);
    lv_label_set_text(V.d_state, st);
    lv_obj_set_style_text_color(V.d_state, cb_col(v->err[0] ? CB_RED : v->degraded[0] ? CB_ORANGE : CB_TEXT_MID), 0);
    lv_label_set_text(V.d_info, v->info);

    bool dms = V.running && ti == 4;
    lv_obj_set_hidden(V.dms_chip, !dms);
    lv_obj_set_hidden(V.dms_sum, !dms);
    if(dms) {
        const char *s = v->dms_status;
        const char *txt = "Waiting";
        cb_color_t col = CB_TEXT_MID;
        if(!strcmp(s, "attentive")) { txt = "Attentive"; col = CB_GREEN; }
        else if(!strcmp(s, "looking_away")) { txt = "Looking away"; col = CB_ORANGE; }
        else if(!strcmp(s, "eyes_not_visible")) { txt = "Eyes not visible"; col = CB_ORANGE; }
        else if(!strcmp(s, "phone_in_hand")) { txt = "Phone in hand"; col = CB_RED; }
        else if(!strcmp(s, "no_driver")) { txt = "No driver"; col = CB_TEXT_LO; }
        lv_label_set_text(V.dms_chip_lbl, txt);
        lv_obj_set_style_bg_color(V.dms_chip, cb_col(col), 0);
        lv_label_set_text(V.dms_sum, v->dms_summary[0] ? v->dms_summary : "Looking for the driver...");
    }
}

/* Source dropdown: cameras, then one "Video: <name>" entry per GET /v1/vision/videos item. */
static void refresh_sources(void)
{
    static net_snapshot_t snap;
    net_snapshot(&snap);
    if(snap.videos_seq == V.seen_vseq) return;
    V.seen_vseq = snap.videos_seq;
    char prev[48];
    src_string(prev, sizeof prev);
    char opts[NET_MAX_VIDEOS * 48 + 80];
    snprintf(opts, sizeof opts, "%s", k_src_name);
    V.n_vid = snap.n_videos;
    int pick = -1;
    for(int i = 0; i < snap.n_videos; i++) {
        snprintf(V.vid_id[i], sizeof V.vid_id[i], "%s", snap.videos[i].id);
        snprintf(V.vid_name[i], sizeof V.vid_name[i], "%s", snap.videos[i].name);
        size_t l = strlen(opts);
        snprintf(opts + l, sizeof opts - l, "\nVideo: %s", snap.videos[i].name);
        char full[48];
        snprintf(full, sizeof full, "video:%s", snap.videos[i].id);
        if(!strcmp(full, prev)) pick = N_CAM + i;
    }
    uint32_t cur = lv_dropdown_get_selected(V.dd_src);
    lv_dropdown_set_options(V.dd_src, opts);
    if(pick >= 0) lv_dropdown_set_selected(V.dd_src, (uint32_t)pick);
    else lv_dropdown_set_selected(V.dd_src, cur < N_CAM ? cur : 0);
    if(V.demo_task && !strcmp(V.demo_task, "lanes") && V.n_vid > 0 && cur < N_CAM && !V.demo_src) {
        lv_dropdown_set_selected(V.dd_src, N_CAM);
        if(V.running) start_stream();
    }
}

static void timer_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    net_vision_take(&V.v);
    const net_vision_t *v = &V.v;
    if(V.running && v->pgen != V.seen_p && v->pix && v->w > 0) {
        V.seen_p = v->pgen;
        show_frame(v);
    }
    if(V.running && v->rgen != V.seen_r) {
        V.seen_r = v->rgen;
        show_overlays(v);
    }
    uint32_t now = lv_tick_get();
    if(now - V.fps_t0 >= 1000) {
        V.pfps = V.fps_n * 1000.0 / (double)(now - V.fps_t0);
        V.fps_n = 0;
        V.fps_t0 = now;
        printf("vision: preview %.1f fps  server %.1f fps  infer %.1f ms  boxes %d  task %s\n", V.pfps, v->fps, v->infer_ms, v->n_boxes, v->task);
        fflush(stdout);
    }
    if(now - V.label_t >= 300) {
        V.label_t = now;
        refresh_sources();
        update_labels(v);
    }
}

/* Test hook (--vtask-cycle): drives the dropdowns through the same VALUE_CHANGED path a tap uses. */
static struct { uint32_t step; } CY;

static void cycle_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    lv_obj_t *dd;
    uint32_t sel;
    int vid = -1;
    switch(CY.step++) {
        case 0: dd = V.dd_task; sel = 1; break;                       /* pose */
        case 1: dd = V.dd_task; sel = 2; break;                       /* segmentation */
        case 2: dd = V.dd_task; sel = 0; break;                       /* detection */
        case 3:
            for(int i = 0; i < V.n_vid; i++)
                if(!strcmp(V.vid_id[i], "road_1")) vid = i;
            dd = V.dd_src; sel = vid >= 0 ? (uint32_t)(N_CAM + vid) : N_CAM; break;
        case 4: dd = V.dd_src; sel = 1; break;                        /* f455 */
        case 5: dd = V.dd_src; sel = 0; break;                        /* c920e */
        default: return;
    }
    printf("vtask-cycle: step %u -> %s #%u\n", (unsigned)(CY.step - 1), dd == V.dd_task ? "task" : "source", (unsigned)sel);
    fflush(stdout);
    lv_dropdown_set_selected(dd, sel);
    lv_obj_send_event(dd, LV_EVENT_VALUE_CHANGED, NULL);
}

void vision_cycle(uint32_t period_ms)
{
    lv_timer_create(cycle_cb, period_ms, NULL);
}

void vision_demo(const char *task, const char *source)
{
    V.demo_task = task;
    V.demo_src = source;
}

static void show(bool on)
{
    if(on) {
        for(int i = 0; i < 6 && V.demo_task; i++)
            if(!strcmp(V.demo_task, k_task_id[i])) lv_dropdown_set_selected(V.dd_task, (uint32_t)i);
        for(int i = 0; i < 2 && V.demo_src; i++)
            if(!strcmp(V.demo_src, k_src_id[i])) lv_dropdown_set_selected(V.dd_src, (uint32_t)i);
        V.fps_t0 = V.label_t = lv_tick_get();
        V.seen_vseq = 0;
        refresh_sources();
        start_stream();
        if(!V.timer) V.timer = lv_timer_create(timer_cb, 33, NULL);
    }
    else {
        stop_stream();
        if(V.timer) {
            lv_timer_delete(V.timer);
            V.timer = NULL;
        }
    }
}

const app_page_t page_vision = {
    .nav = "Computer Vision", .icon = UI_ICON_EYE, .eyebrow = "EDGE AI / VISION", .title = "Computer Vision",
    .subtitle = "Live camera previews with detections drawn on the Hailo-10H.",
    .build = build, .show = show,
};
