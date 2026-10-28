#include "app.h"
#include "display.h"
#include <stdio.h>
#include <string.h>

extern const lv_image_dsc_t logo_brain_chip;

static const app_page_t *const k_pages[] = { &page_overview, &page_voice, &page_vision, &page_chat, &page_face, &page_device, &page_inspector };
#define N_PAGES ((int)(sizeof k_pages / sizeof *k_pages))

static struct {
    lv_obj_t *nav[N_PAGES], *page[N_PAGES], *content, *eyebrow, *title, *subtitle;
    lv_obj_t *pill, *pill_dot, *pill_lbl, *temp_chip, *side_state, *side_url, *side_dot;
    lv_obj_t *sink;
    int cur;
    bool quit;
    net_snapshot_t snap;
    uint32_t last_seq;
} A = { .cur = -1 };

static net_snapshot_t s_tmp;

/* ---------- helpers ---------- */

lv_obj_t *app_flow(lv_obj_t *parent, int32_t gap)
{
    lv_obj_t *o = ui_row(parent, gap);
    lv_obj_set_flex_flow(o, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_style_pad_row(o, gap, 0);
    lv_obj_set_flex_align(o, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    return o;
}

lv_obj_t *app_card(lv_obj_t *parent, const char *title)
{
    lv_obj_t *c = ui_card(parent);
    lv_obj_set_style_pad_all(c, 16, 0);
    lv_obj_set_style_pad_row(c, 10, 0);
    if(title) {
        lv_obj_t *l = lv_label_create(c);
        lv_obj_add_style(l, &cb_st_label, 0);
        lv_label_set_text(l, title);
    }
    return c;
}

lv_obj_t *app_tile(lv_obj_t *parent, const char *label, lv_obj_t **value, lv_obj_t **bar, int32_t w_pct)
{
    lv_obj_t *c = ui_card(parent);
    lv_obj_set_width(c, lv_pct(w_pct - 2));
    lv_obj_set_flex_grow(c, 1);
    lv_obj_set_style_pad_all(c, 16, 0);
    lv_obj_set_style_pad_row(c, 6, 0);
    ui_label(c, label, CB_F_BODY, CB_TEXT_MID);
    lv_obj_t *v = ui_label(c, "-", CB_F_TEMP, CB_TEXT_HI);
    if(value) *value = v;
    if(bar) {
        lv_obj_t *b = lv_bar_create(c);
        lv_obj_set_size(b, lv_pct(100), 6);
        lv_bar_set_range(b, 0, 100);
        lv_obj_set_style_bg_color(b, cb_col(CB_HAIRLINE), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(b, 3, 0);
        lv_obj_set_style_bg_color(b, cb_col(CB_CYAN), LV_PART_INDICATOR);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_PART_INDICATOR);
        lv_obj_set_style_radius(b, 3, LV_PART_INDICATOR);
        *bar = b;
    }
    return c;
}

void app_fmt_ms(char *out, size_t cap, double ms)
{
    if(ms >= 1000) snprintf(out, cap, "%.2f s", ms / 1000.0);
    else if(ms >= 10) snprintf(out, cap, "%.0f ms", ms);
    else snprintf(out, cap, "%.1f ms", ms);
}

/* Column widths follow the container: the window starts at its default size and is resized to the desktop when
   fullscreen, so widths computed at build time would be stale. */
typedef struct { lv_obj_t *table; const int *pct; int n; } fit_t;

static void table_fit(lv_event_t *e)
{
    fit_t *f = lv_event_get_user_data(e);
    int32_t w = lv_obj_get_content_width(lv_event_get_target_obj(e));
    if(w < 200) return;
    for(int i = 0; i < f->n; i++) lv_table_set_column_width(f->table, (uint32_t)i, w * f->pct[i] / 100);
}

void app_table_fit(lv_obj_t *table, lv_obj_t *container, const int *pct, int n)
{
    fit_t *f = lv_malloc(sizeof *f);
    *f = (fit_t){ table, pct, n };
    lv_obj_add_event_cb(container, table_fit, LV_EVENT_SIZE_CHANGED, f);
    lv_obj_add_event_cb(container, table_fit, LV_EVENT_STYLE_CHANGED, f);
}

int32_t app_content_w(void) { return lv_obj_get_width(A.content) - 4; }
int app_page(void) { return A.cur; }
int app_page_count(void) { return N_PAGES; }
void app_request_quit(void) { A.quit = true; }
bool app_quit_requested(void) { return A.quit; }
lv_obj_t *app_key_sink(void) { return A.sink; }

/* ---------- navigation ---------- */

static void style_nav(int i, bool on)
{
    lv_obj_t *n = A.nav[i];
    lv_obj_set_style_bg_color(n, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(n, on ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(n, cb_col(CB_CYAN), 0);
    lv_obj_set_style_border_width(n, on ? 3 : 0, 0);
    lv_obj_set_style_border_side(n, LV_BORDER_SIDE_LEFT, 0);
    cb_color_t tc = on ? CB_TEXT_HI : CB_TEXT_MID;
    ui_icon_set_color(lv_obj_get_child(n, 0), on ? CB_CYAN : CB_TEXT_MID);
    lv_obj_set_style_text_color(lv_obj_get_child(n, 1), cb_col(tc), 0);
}

void app_goto(int idx)
{
    if(idx < 0 || idx >= N_PAGES || idx == A.cur) return;
    if(A.cur >= 0) {
        lv_obj_set_hidden(A.page[A.cur], true);
        style_nav(A.cur, false);
        if(k_pages[A.cur]->show) k_pages[A.cur]->show(false);
    }
    A.cur = idx;
    lv_obj_set_hidden(A.page[idx], false);
    style_nav(idx, true);
    lv_label_set_text(A.eyebrow, k_pages[idx]->eyebrow);
    lv_label_set_text(A.title, k_pages[idx]->title);
    lv_label_set_text(A.subtitle, k_pages[idx]->subtitle);
    lv_obj_scroll_to_y(A.page[idx], 0, LV_ANIM_OFF);
    if(A.sink) lv_group_focus_obj(A.sink);
    if(k_pages[idx]->show) k_pages[idx]->show(true);
    if(k_pages[idx]->tick) k_pages[idx]->tick(&A.snap);
}

static void nav_click(lv_event_t *e)
{
    app_goto((int)(intptr_t)lv_event_get_user_data(e));
}

/* wheel notches arrive as RIGHT (down) / LEFT (up) keys; scroll the visible page by a fixed step */
static void wheel_cb(lv_event_t *e)
{
    uint32_t key = lv_event_get_key(e);
    if(A.cur < 0 || (key != LV_KEY_RIGHT && key != LV_KEY_LEFT)) return;
    lv_obj_scroll_by_bounded(A.page[A.cur], 0, key == LV_KEY_RIGHT ? -90 : 90, LV_ANIM_ON);
}

/* digits switch pages, Esc quits (when no text field has the focus) */
static void key_cb(lv_event_t *e)
{
    uint32_t k = lv_event_get_key(e);
    if(k >= '1' && k < (uint32_t)('1' + N_PAGES)) app_goto((int)(k - '1'));
    else if(k == LV_KEY_ESC) A.quit = true;
}

/* ---------- 2 Hz refresh ---------- */

static void refresh_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    net_snapshot(&A.snap);
    net_snapshot_t *s = &A.snap;
    bool on = s->online;
    lv_obj_set_style_bg_color(A.pill_dot, cb_col(on ? CB_GREEN : CB_RED), 0);
    lv_label_set_text(A.pill_lbl, on ? "Server Online" : "Server Offline");
    lv_obj_set_style_text_color(A.pill_lbl, cb_col(on ? CB_GREEN : CB_RED), 0);
    lv_obj_set_style_bg_color(A.side_dot, cb_col(on ? CB_GREEN : CB_RED), 0);
    lv_label_set_text(A.side_state, on ? "Streaming telemetry" : "Waiting for server");
    char b[48];
    snprintf(b, sizeof b, "Pi %.0f C  \xC2\xB7  NPU %.0f C", s->temp_c, s->npu_temp);
    if(s->temp_c > 0) lv_label_set_text(lv_obj_get_child(A.temp_chip, -1), b);
    if(A.cur >= 0 && k_pages[A.cur]->tick) k_pages[A.cur]->tick(s);
}

/* ---------- shell ---------- */

static lv_obj_t *nav_item(lv_obj_t *parent, int i)
{
    lv_obj_t *n = ui_plain(parent);
    lv_obj_set_size(n, lv_pct(100), 46);
    lv_obj_set_style_radius(n, 8, 0);
    lv_obj_set_style_pad_left(n, 16, 0);
    lv_obj_set_flex_flow(n, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(n, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_clickable(n, true);
    ui_icon(n, k_pages[i]->icon, CB_TEXT_MID);
    lv_obj_t *nl = ui_label(n, k_pages[i]->nav, CB_F_VALUE, CB_TEXT_MID);
    lv_obj_set_style_margin_left(nl, 14, 0);
    lv_obj_add_event_cb(n, nav_click, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    return n;
}

void app_build(lv_obj_t *root, const app_cfg_t *cfg)
{
    cb_theme_set_reduce_motion(cfg->reduce_motion);
    lv_obj_set_flex_flow(root, LV_FLEX_FLOW_ROW);

    /* sidebar */
    lv_obj_t *side = ui_plain(root);
    lv_obj_set_size(side, 280, lv_pct(100));
    lv_obj_set_style_bg_color(side, cb_col(CB_SURFACE), 0);
    lv_obj_set_style_bg_opa(side, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(side, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(side, 1, 0);
    lv_obj_set_style_border_side(side, LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_set_style_pad_all(side, 18, 0);
    lv_obj_set_style_pad_row(side, 6, 0);
    lv_obj_set_flex_flow(side, LV_FLEX_FLOW_COLUMN);

    lv_obj_t *logo = ui_row(side, 10);
    lv_obj_set_style_pad_bottom(logo, 18, 0);
    lv_obj_t *mark = ui_plain(logo);
    lv_obj_set_size(mark, 48, 48);
    lv_obj_set_style_radius(mark, 10, 0);
    lv_obj_set_style_bg_color(mark, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(mark, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(mark, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(mark, 1, 0);
    lv_obj_t *glyph = lv_image_create(mark);
    lv_image_set_src(glyph, &logo_brain_chip);
    lv_obj_center(glyph);
    lv_obj_t *names = ui_col(logo, 0);
    lv_obj_set_width(names, LV_SIZE_CONTENT);
    ui_label(names, "AI BOX Studio", CB_F_TITLE, CB_TEXT_HI);
    lv_obj_t *sub = ui_label(names, "EDGE INFERENCE SERVER", CB_F_SMALL, CB_TEXT_MID);
    lv_obj_set_style_text_letter_space(sub, 1, 0);

    lv_obj_t *ws = ui_label(side, "WORKSPACE", CB_F_SMALL, CB_TEXT_LO);
    lv_obj_set_style_text_letter_space(ws, 2, 0);
    lv_obj_set_style_pad_left(ws, 16, 0);
    for(int i = 0; i < N_PAGES; i++) {
        if(k_pages[i] == &page_device) {
            lv_obj_t *sy = ui_label(side, "SYSTEM", CB_F_SMALL, CB_TEXT_LO);
            lv_obj_set_style_text_letter_space(sy, 2, 0);
            lv_obj_set_style_pad_left(sy, 16, 0);
            lv_obj_set_style_pad_top(sy, 14, 0);
        }
        A.nav[i] = nav_item(side, i);
    }
    lv_obj_t *grow = ui_plain(side);
    lv_obj_set_size(grow, 1, 1);
    lv_obj_set_flex_grow(grow, 1);
    lv_obj_t *foot = ui_col(side, 4);
    lv_obj_set_style_border_color(foot, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(foot, 1, 0);
    lv_obj_set_style_border_side(foot, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_pad_top(foot, 12, 0);
    lv_obj_t *fr = ui_row(foot, 8);
    A.side_dot = ui_dot(fr, CB_RED, false);
    A.side_state = ui_label(fr, "Waiting for server", CB_F_LABEL, CB_TEXT_2);
    ui_label(foot, "Hailo-10H  \xC2\xB7  Raspberry Pi 5", CB_F_LABEL, CB_TEXT_MID);
    A.side_url = ui_label_line(foot, net_base_url(), CB_F_MONO_S, CB_TEXT_MID);

    /* content */
    lv_obj_t *main = ui_plain(root);
    lv_obj_set_height(main, lv_pct(100));
    lv_obj_set_flex_grow(main, 1);
    lv_obj_set_style_pad_all(main, 28, 0);
    lv_obj_set_style_pad_row(main, 16, 0);
    lv_obj_set_flex_flow(main, LV_FLEX_FLOW_COLUMN);

    lv_obj_t *top = ui_row(main, 12);
    lv_obj_t *tl = ui_col(top, 4);
    lv_obj_set_width(tl, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(tl, 1);
    A.eyebrow = ui_label(tl, "", CB_F_LABEL, CB_TEXT_MID);
    lv_obj_set_style_text_letter_space(A.eyebrow, 2, 0);
    A.title = ui_label(tl, "", CB_F_TEMP, CB_TEXT_HI);
    A.subtitle = ui_label(tl, "", CB_F_BODY, CB_TEXT_MID);
    A.temp_chip = ui_chip(top, "Pi - C", CB_ORANGE, false);
    A.pill = ui_chip(top, "Server Offline", CB_RED, false);
    lv_obj_set_style_bg_color(A.pill, cb_col(CB_SURFACE_HI), 0);
    A.pill_dot = lv_obj_get_child(A.pill, 0);
    A.pill_lbl = ui_chip_label(A.pill);
    lv_obj_set_style_text_font(A.pill_lbl, cb_font(CB_F_BODY), 0);

    A.content = ui_plain(main);
    lv_obj_set_width(A.content, lv_pct(100));
    lv_obj_set_flex_grow(A.content, 1);
    for(int i = 0; i < N_PAGES; i++) {
        lv_obj_t *p = ui_plain(A.content);
        lv_obj_set_size(p, lv_pct(100), lv_pct(100));
        lv_obj_set_flex_flow(p, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_style_pad_row(p, 14, 0);
        lv_obj_set_style_pad_right(p, 4, 0);
        lv_obj_set_scrollbar_mode(p, LV_SCROLLBAR_MODE_AUTO);
        lv_obj_set_scroll_dir(p, LV_DIR_VER);
        lv_obj_set_scrollable(p, true);
        lv_obj_set_hidden(p, true);
        A.page[i] = p;
    }

    /* key sink: digits switch pages, Esc quits */
    A.sink = lv_obj_create(root);
    lv_obj_remove_style_all(A.sink);
    lv_obj_set_size(A.sink, 0, 0);
    lv_obj_set_floating(A.sink, true);
    lv_obj_add_event_cb(A.sink, key_cb, LV_EVENT_KEY, NULL);
    lv_group_add_obj(cb_display_key_group(), A.sink);
    lv_group_focus_obj(A.sink);

    /* mouse wheel: scrolls the visible page (editing mode turns rotation into LEFT/RIGHT keys) */
    lv_obj_t *wheel_sink = lv_obj_create(root);
    lv_obj_remove_style_all(wheel_sink);
    lv_obj_set_size(wheel_sink, 0, 0);
    lv_obj_set_floating(wheel_sink, true);
    lv_obj_add_event_cb(wheel_sink, wheel_cb, LV_EVENT_KEY, NULL);
    lv_group_add_obj(cb_display_wheel_group(), wheel_sink);
    lv_group_focus_obj(wheel_sink);
    lv_group_set_editing(cb_display_wheel_group(), true);

    lv_obj_update_layout(root);
    for(int i = 0; i < N_PAGES; i++) k_pages[i]->build(A.page[i]);
    A.snap = s_tmp;
    app_goto(cfg->page >= 0 && cfg->page < N_PAGES ? cfg->page : 0);
    lv_timer_create(refresh_cb, 500, NULL);
    refresh_cb(NULL);
}
