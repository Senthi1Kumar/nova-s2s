#include "app.h"
#include "display.h"
#include <stdio.h>
#include <string.h>

#define PCOUNT 3
#define OUTLINE_PTS 33

static struct {
    lv_obj_t *stage, *view, *canvas, *wait, *outline;
    lv_point_precise_t outline_pts[OUTLINE_PTS];
    lv_obj_t *status, *mode, *ta, *role, *users_dd, *users_text, *kb;
    lv_obj_t *confirm_box, *confirm_text;
    lv_draw_buf_t db[PCOUNT];
    const uint16_t *db_pix[PCOUNT];
    net_vision_t v;
    net_face_t f;
    lv_timer_t *timer;
    uint32_t seen_p, seen_r, seen_gen, seen_users, green_until;
    int cur_w, cur_h, max_w, max_h, confirm_mode;
    float scale;
    char pending_name[32], pending_role[12], pending_user[32];
    char user_ids[NET_MAX_FACE_USERS][32];
    bool running;
} F;

static lv_obj_t *button(lv_obj_t *parent, const char *text, bool primary, int32_t w)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, w, 44);
    lv_obj_set_style_bg_color(b, cb_col(primary ? CB_CYAN : CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(b, 9, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_color(b, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(b, primary ? 0 : 1, 0);
    lv_obj_t *label = ui_label(b, text, CB_F_VALUE, primary ? CB_BASE : CB_TEXT_HI);
    lv_obj_center(label);
    return b;
}

static void set_status(const char *text, cb_color_t color)
{
    lv_label_set_text(F.status, text);
    lv_obj_set_style_text_color(F.status, cb_col(color), 0);
}

static void hide_outline(void)
{
    lv_obj_set_hidden(F.outline, true);
}

static void clear_view(const char *text)
{
    lv_obj_set_hidden(F.view, true);
    hide_outline();
    F.cur_w = F.cur_h = 0;
    lv_label_set_text(F.wait, text);
    lv_obj_set_hidden(F.wait, false);
    lv_obj_invalidate(F.stage);
}

static void start_stream(void)
{
    clear_view("Starting F455 preview...");
    net_vision_start("f455", "face_track", 10, 480);
    F.running = true;
    net_vision_take(&F.v);
    F.seen_p = F.v.pgen;
    F.seen_r = F.v.rgen;
}

static void stop_stream(void)
{
    net_vision_stop();
    F.running = false;
    clear_view("Preview stopped");
}

static void kb_set(bool on)
{
    lv_obj_set_hidden(F.kb, !on);
}

static void drop_focus(void)
{
    kb_set(false);
    lv_group_focus_obj(app_key_sink());
}

static void hide_confirm(void)
{
    F.confirm_mode = 0;
    lv_obj_set_hidden(F.confirm_box, true);
}

static void show_confirm(int mode, const char *text)
{
    F.confirm_mode = mode;
    lv_label_set_text(F.confirm_text, text);
    lv_obj_set_hidden(F.confirm_box, false);
}

static void auth_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    hide_confirm();
    if(net_face_authenticate()) set_status("Authenticating - keep looking at the F455", CB_ORANGE);
}

static void enroll_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    const char *name = lv_textarea_get_text(F.ta);
    if(!name[0]) {
        set_status("Enter a name before enrolling", CB_RED);
        return;
    }
    snprintf(F.pending_name, sizeof F.pending_name, "%s", name);
    snprintf(F.pending_role, sizeof F.pending_role, "%s",
             lv_dropdown_get_selected(F.role) == 0 ? "driver" : "passenger");
    hide_confirm();
    drop_focus();
    if(net_face_enroll(F.pending_name, F.pending_role, false))
        set_status("Checking for duplicates, then enrolling - follow the camera hints", CB_ORANGE);
}

static void delete_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    uint32_t selected = lv_dropdown_get_selected(F.users_dd);
    if(selected >= (uint32_t)F.f.n_users) return;
    snprintf(F.pending_user, sizeof F.pending_user, "%s", F.user_ids[selected]);
    char text[160];
    snprintf(text, sizeof text, "Delete %s (%s) from the F455?", F.f.users[selected].name, F.f.users[selected].role);
    show_confirm(3, text);
}

static void delete_all_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if(F.f.n_users) show_confirm(2, "Delete every enrolled face template and roster entry? This cannot be undone.");
}

static void refresh_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    net_face_refresh();
}

static void confirm_yes_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    int mode = F.confirm_mode;
    hide_confirm();
    bool accepted = false;
    if(mode == 1) {
        accepted = net_face_enroll(F.pending_name, F.pending_role, true);
        if(accepted) set_status("Replacing the driver - follow the camera hints", CB_ORANGE);
    }
    else if(mode == 2) {
        accepted = net_face_delete("");
        if(accepted) set_status("Deleting all enrolled users...", CB_ORANGE);
    }
    else if(mode == 3) {
        accepted = net_face_delete(F.pending_user);
        if(accepted) set_status("Deleting the selected user...", CB_ORANGE);
    }
}

static void confirm_no_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    hide_confirm();
    set_status("Cancelled", CB_TEXT_MID);
}

static void ta_cb(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    if(code == LV_EVENT_FOCUSED) kb_set(true);
    else if(code == LV_EVENT_DEFOCUSED) kb_set(false);
    else if(code == LV_EVENT_KEY && lv_event_get_key(e) == LV_KEY_ESC) drop_focus();
    else if(code == LV_EVENT_READY) enroll_cb(e);
}

static void kb_cb(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    if(code == LV_EVENT_READY) enroll_cb(e);
    else if(code == LV_EVENT_CANCEL) drop_focus();
}

static void style_dropdown(lv_obj_t *d, int32_t width)
{
    lv_obj_set_width(d, width);
    lv_obj_set_style_text_font(d, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_bg_color(d, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(d, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_color(d, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(d, 1, 0);
    lv_obj_set_style_radius(d, 8, 0);
    lv_obj_t *list = lv_dropdown_get_list(d);
    lv_obj_set_style_bg_color(list, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(list, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(list, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(list, cb_font(CB_F_BODY), 0);
}

static void build(lv_obj_t *page)
{
    lv_obj_t *preview = app_card(page, "LIVE F455 / FACE TRACKING");
    lv_obj_set_width(preview, lv_pct(100));
    F.stage = ui_plain(preview);
    lv_obj_set_size(F.stage, lv_pct(100), 430);
    lv_obj_set_style_bg_color(F.stage, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(F.stage, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(F.stage, 10, 0);
    lv_obj_set_flex_flow(F.stage, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(F.stage, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    F.view = ui_plain(F.stage);
    lv_obj_set_size(F.view, 240, 426);
    lv_obj_set_overflow_visible(F.view, false);
    F.canvas = lv_canvas_create(F.view);
    lv_obj_set_pos(F.canvas, 0, 0);
    F.outline = lv_line_create(F.view);
    lv_obj_set_style_line_color(F.outline, lv_color_white(), 0);
    lv_obj_set_style_line_width(F.outline, 3, 0);
    lv_obj_set_style_line_rounded(F.outline, true, 0);
    lv_obj_set_hidden(F.outline, true);
    F.wait = ui_label(F.stage, "Starting F455 preview...", CB_F_VALUE, CB_TEXT_MID);
    lv_obj_set_floating(F.wait, true);
    lv_obj_align(F.wait, LV_ALIGN_CENTER, 0, 0);
    lv_obj_t *hint = ui_label(preview,
        "The white oval follows the Hailo SCRFD face box and turns green after authentication. Templates, matching and anti-spoofing stay inside the F455.",
        CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_width(hint, lv_pct(100));
    lv_label_set_long_mode(hint, LV_LABEL_LONG_MODE_WRAP);

    lv_obj_t *auth = app_card(page, "AUTHENTICATE");
    lv_obj_set_width(auth, lv_pct(100));
    lv_obj_t *ab = button(auth, "Authenticate", true, 180);
    lv_obj_add_event_cb(ab, auth_cb, LV_EVENT_CLICKED, NULL);
    F.mode = ui_label(auth, "Device mode and security level appear after authentication.", CB_F_BODY, CB_TEXT_MID);
    F.status = ui_label(auth, "Ready", CB_F_TITLE, CB_TEXT_HI);
    lv_obj_set_width(F.status, lv_pct(100));
    lv_label_set_long_mode(F.status, LV_LABEL_LONG_MODE_WRAP);

    lv_obj_t *enroll = app_card(page, "ENROLL A DRIVER OR PASSENGER");
    lv_obj_set_width(enroll, lv_pct(100));
    lv_obj_t *form = app_flow(enroll, 10);
    F.ta = lv_textarea_create(form);
    lv_textarea_set_one_line(F.ta, true);
    lv_textarea_set_placeholder_text(F.ta, "Full name");
    lv_obj_set_size(F.ta, 300, 46);
    lv_obj_set_style_bg_color(F.ta, cb_col(CB_SURFACE), 0);
    lv_obj_set_style_bg_opa(F.ta, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(F.ta, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(F.ta, cb_font(CB_F_VALUE), 0);
    lv_obj_set_style_border_color(F.ta, cb_col(CB_CYAN), LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(F.ta, 1, LV_STATE_FOCUSED);
    lv_obj_set_style_radius(F.ta, 8, 0);
    lv_group_add_obj(cb_display_key_group(), F.ta);
    lv_obj_add_event_cb(F.ta, ta_cb, LV_EVENT_ALL, NULL);
    F.role = lv_dropdown_create(form);
    lv_dropdown_set_options(F.role, "Driver\nPassenger");
    style_dropdown(F.role, 160);
    lv_obj_t *eb = button(form, "Enroll", true, 120);
    lv_obj_add_event_cb(eb, enroll_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *note = ui_label(enroll,
        "Duplicate faces and duplicate name/role pairs are rejected. Replacing a driver requires confirmation.",
        CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_width(note, lv_pct(100));
    lv_label_set_long_mode(note, LV_LABEL_LONG_MODE_WRAP);

    F.confirm_box = ui_card(enroll);
    F.confirm_text = ui_label(F.confirm_box, "", CB_F_VALUE, CB_ORANGE);
    lv_obj_set_width(F.confirm_text, lv_pct(100));
    lv_label_set_long_mode(F.confirm_text, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_t *confirm_row = ui_row(F.confirm_box, 10);
    lv_obj_t *yes = button(confirm_row, "Confirm", true, 130);
    lv_obj_add_event_cb(yes, confirm_yes_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *no = button(confirm_row, "Cancel", false, 130);
    lv_obj_add_event_cb(no, confirm_no_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_set_hidden(F.confirm_box, true);

    lv_obj_t *roster = app_card(page, "ENROLLED USERS");
    lv_obj_set_width(roster, lv_pct(100));
    F.users_dd = lv_dropdown_create(roster);
    lv_dropdown_set_options(F.users_dd, "Loading users...");
    style_dropdown(F.users_dd, 420);
    lv_obj_t *actions = app_flow(roster, 10);
    lv_obj_t *del = button(actions, "Delete selected", false, 170);
    lv_obj_add_event_cb(del, delete_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *del_all = button(actions, "Delete all", false, 140);
    lv_obj_add_event_cb(del_all, delete_all_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *refresh = button(actions, "Refresh", false, 120);
    lv_obj_add_event_cb(refresh, refresh_cb, LV_EVENT_CLICKED, NULL);
    F.users_text = ui_label(roster, "", CB_F_BODY, CB_TEXT_MID);
    lv_obj_set_width(F.users_text, lv_pct(100));
    lv_label_set_long_mode(F.users_text, LV_LABEL_LONG_MODE_WRAP);

    F.kb = lv_keyboard_create(page);
    lv_keyboard_set_textarea(F.kb, F.ta);
    lv_obj_set_size(F.kb, lv_pct(100), 250);
    lv_obj_set_style_text_font(F.kb, &lv_font_montserrat_20, LV_PART_ITEMS);
    lv_obj_add_event_cb(F.kb, kb_cb, LV_EVENT_ALL, NULL);
    lv_obj_set_hidden(F.kb, true);
}

static void fit_view(int w, int h)
{
    lv_obj_update_layout(F.stage);
    int mw = lv_obj_get_content_width(F.stage), mh = lv_obj_get_content_height(F.stage);
    if(w == F.cur_w && h == F.cur_h && mw == F.max_w && mh == F.max_h) return;
    F.cur_w = w;
    F.cur_h = h;
    F.max_w = mw;
    F.max_h = mh;
    float sx = (float)mw / (float)w, sy = (float)mh / (float)h;
    F.scale = sx < sy ? sx : sy;
    if(F.scale > 3.0f) F.scale = 3.0f;
    lv_obj_set_size(F.view, (int32_t)(w * F.scale), (int32_t)(h * F.scale));
    lv_image_set_pivot(F.canvas, 0, 0);
    lv_image_set_scale(F.canvas, (uint32_t)(256.0f * F.scale));
}

static void show_frame(const net_vision_t *v)
{
    int k = -1;
    for(int i = 0; i < PCOUNT; i++)
        if(F.db_pix[i] == v->pix) k = i;
    if(k < 0)
        for(int i = 0; i < PCOUNT && k < 0; i++)
            if(!F.db_pix[i]) { k = i; F.db_pix[i] = v->pix; }
    if(k < 0) k = 0;
    fit_view(v->w, v->h);
    lv_draw_buf_init(&F.db[k], (uint32_t)v->w, (uint32_t)v->h, LV_COLOR_FORMAT_RGB565,
                     (uint32_t)v->w * 2, (void *)v->pix, (uint32_t)(v->w * v->h * 2));
    lv_canvas_set_draw_buf(F.canvas, &F.db[k]);
    lv_obj_invalidate(F.canvas);
    lv_obj_set_hidden(F.view, false);
    lv_obj_set_hidden(F.wait, true);
}

static void show_outline(const net_vision_t *v)
{
    if(v->n_boxes < 1 || F.cur_w <= 0) {
        hide_outline();
        return;
    }
    float sx = F.scale, sy = F.scale;
    if(v->img_w > 0) sx *= (float)F.cur_w / (float)v->img_w;
    if(v->img_h > 0) sy *= (float)F.cur_h / (float)v->img_h;
    const net_box_t *b = &v->boxes[0];
    float w = b->x2 - b->x1, h = b->y2 - b->y1;
    for(int i = 0; i < OUTLINE_PTS; i++) {
        int32_t angle = i * 360 / (OUTLINE_PTS - 1);
        float cx = (float)lv_trigo_cos(angle) / 32767.0f;
        float cy = (float)lv_trigo_sin(angle) / 32767.0f;
        F.outline_pts[i].x = (b->x1 + w * (0.5f + 0.49f * cx)) * sx;
        F.outline_pts[i].y = (b->y1 + h * (0.5f + 0.49f * cy)) * sy;
    }
    lv_line_set_points(F.outline, F.outline_pts, OUTLINE_PTS);
    lv_obj_set_style_line_color(F.outline, cb_col(lv_tick_get() < F.green_until ? CB_GREEN : CB_TEXT_HI), 0);
    lv_obj_set_hidden(F.outline, false);
}

static void refresh_users(void)
{
    char opts[NET_MAX_FACE_USERS * 48 + 32];
    char text[NET_MAX_FACE_USERS * 72 + 32];
    opts[0] = text[0] = '\0';
    if(!F.f.n_users) {
        snprintf(opts, sizeof opts, "No enrolled users");
        snprintf(text, sizeof text, "No enrolled users.");
    }
    for(int i = 0; i < F.f.n_users; i++) {
        snprintf(F.user_ids[i], sizeof F.user_ids[i], "%s", F.f.users[i].user_id);
        size_t n = strlen(opts);
        snprintf(opts + n, sizeof opts - n, "%s%s (%s)", i ? "\n" : "", F.f.users[i].name, F.f.users[i].role);
        n = strlen(text);
        snprintf(text + n, sizeof text - n, "%s%s - %s%s", i ? "\n" : "", F.f.users[i].name,
                 F.f.users[i].role, F.f.users[i].on_sensor ? "" : " (roster only)");
    }
    lv_dropdown_set_options(F.users_dd, opts);
    lv_label_set_text(F.users_text, text);
}

static void poll_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    net_vision_take(&F.v);
    if(F.running && F.v.pgen != F.seen_p && F.v.pix && F.v.w > 0) {
        F.seen_p = F.v.pgen;
        show_frame(&F.v);
    }
    if(F.running && F.v.rgen != F.seen_r) {
        F.seen_r = F.v.rgen;
        show_outline(&F.v);
        if(F.v.err[0]) lv_label_set_text(F.wait, F.v.err);
    }
    net_face_take(&F.f);
    if(F.f.users_gen != F.seen_users) {
        F.seen_users = F.f.users_gen;
        refresh_users();
    }
    if(F.f.gen == F.seen_gen || F.f.busy) return;
    F.seen_gen = F.f.gen;
    if(!F.f.done) return;
    if(F.f.driver_exists) {
        char text[180];
        snprintf(text, sizeof text, "Replace %s with %s as the driver? The old driver template will be deleted.",
                 F.f.existing_name, F.pending_name);
        show_confirm(1, text);
        set_status("Driver replacement needs confirmation", CB_ORANGE);
        return;
    }
    hide_confirm();
    set_status(F.f.message, F.f.ok ? CB_GREEN : CB_RED);
    if(F.f.ok && F.f.authenticated) {
        F.green_until = lv_tick_get() + 6000;
        show_outline(&F.v);
        char mode[96];
        snprintf(mode, sizeof mode, "Mode %s  |  Security %s", F.f.flow, F.f.security);
        lv_label_set_text(F.mode, mode);
    }
    if(F.f.ok && F.f.name[0] && !F.f.authenticated) lv_textarea_set_text(F.ta, "");
}

static void show(bool on)
{
    if(on) {
        start_stream();
        net_face_refresh();
        if(!F.timer) F.timer = lv_timer_create(poll_cb, 33, NULL);
    }
    else {
        stop_stream();
        drop_focus();
        hide_confirm();
        if(F.timer) {
            lv_timer_delete(F.timer);
            F.timer = NULL;
        }
    }
}

const app_page_t page_face = {
    .nav = "Face ID", .icon = UI_ICON_FACE, .eyebrow = "EDGE AI / REALSENSE ID", .title = "Face ID",
    .subtitle = "Enrollment, deletion and secure facial authentication on the Intel F455.",
    .build = build, .show = show,
};
