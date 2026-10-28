#include "app.h"
#include "display.h"
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>

/* Local Chat: model dropdown from /v1/models, one WS /v1/chat/stream connection per question (net.c), tokens
   appended to the assistant bubble as they arrive. The keyboard opens only after the input is tapped. */

#define MAXMSG 24
#define MAX_TOKENS 512
#define PICKER_MAX 8

typedef struct {
    bool user;
    char *text;
    lv_obj_t *body;
} msg_t;

static struct {
    lv_obj_t *dd, *state, *log, *stats, *ta, *send, *send_lbl, *kb, *picker, *attach_name;
    lv_obj_t *file_btn[PICKER_MAX];
    char image_path[256], files[PICKER_MAX][256], file_names[PICKER_MAX][64];
    int n_files;
    msg_t m[MAXMSG];
    int n;
    bool waiting;           /* a reply is streaming into m[n-1] */
    uint32_t seen_gen, seen_models;
    char ids[NET_MAX_MODELS][40];
    int n_ids;
    lv_timer_t *timer;
} C;

static void drop_focus(void);

static void scroll_end(void)
{
    lv_obj_update_layout(C.log);
    lv_obj_scroll_to_y(C.log, LV_COORD_MAX, LV_ANIM_OFF);
}

static void add_msg(bool user, const char *text)
{
    if(C.n == MAXMSG) {   /* drop the oldest exchange */
        for(int i = 0; i < 2; i++) {
            lv_obj_delete(lv_obj_get_parent(C.m[i].body));
            free(C.m[i].text);
        }
        memmove(&C.m[0], &C.m[2], (size_t)(MAXMSG - 2) * sizeof(msg_t));
        C.n -= 2;
    }
    lv_obj_t *b = ui_card(C.log);
    lv_obj_set_style_pad_all(b, 14, 0);
    lv_obj_set_style_bg_color(b, cb_col(user ? CB_SURFACE_HI : CB_SURFACE), 0);
    if(user) {
        lv_obj_set_style_border_color(b, cb_col(CB_CYAN), 0);
        lv_obj_set_style_border_opa(b, 120, 0);
    }
    ui_label(b, user ? "YOU" : "ASSISTANT", CB_F_SMALL, user ? CB_CYAN : CB_GREEN);
    lv_obj_t *body = ui_label(b, text, CB_F_VALUE, CB_TEXT_HI);
    lv_obj_set_width(body, lv_pct(100));
    lv_label_set_long_mode(body, LV_LABEL_LONG_MODE_WRAP);
    C.m[C.n] = (msg_t){ user, strdup(text), body };
    C.n++;
    scroll_end();
}

static void set_waiting(bool w)
{
    C.waiting = w;
    lv_label_set_text(C.send_lbl, w ? "Stop" : "Send");
}

static bool image_name(const char *name)
{
    const char *dot = strrchr(name, '.');
    return dot && (!strcasecmp(dot, ".jpg") || !strcasecmp(dot, ".jpeg") || !strcasecmp(dot, ".png"));
}

static void scan_files(void)
{
    char dir[192];
    snprintf(dir, sizeof dir, "%s/Pictures", getenv("HOME") ? getenv("HOME") : "/home/cariad");
    DIR *dp = opendir(dir);
    C.n_files = 0;
    if(dp) {
        struct dirent *ent;
        while(C.n_files < PICKER_MAX && (ent = readdir(dp))) {
            if(!image_name(ent->d_name)) continue;
            char path[512];
            snprintf(path, sizeof path, "%s/%s", dir, ent->d_name);
            struct stat st;
            if(stat(path, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size <= 0 || st.st_size > 4 * 1024 * 1024) continue;
            if(strlen(path) >= sizeof C.files[0]) continue;
            snprintf(C.files[C.n_files], sizeof C.files[0], "%s", path);
            snprintf(C.file_names[C.n_files], sizeof C.file_names[0], "%.63s", ent->d_name);
            C.n_files++;
        }
        closedir(dp);
    }
    for(int i = 0; i < PICKER_MAX; i++) {
        lv_obj_set_hidden(C.file_btn[i], i >= C.n_files);
        if(i < C.n_files) lv_label_set_text(lv_obj_get_child(C.file_btn[i], 0), C.file_names[i]);
    }
}

static void file_cb(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if(i < 0 || i >= C.n_files) return;
    snprintf(C.image_path, sizeof C.image_path, "%s", C.files[i]);
    char text[120];
    snprintf(text, sizeof text, "Attached: %s", C.file_names[i]);
    lv_label_set_text(C.attach_name, text);
    lv_obj_set_hidden(C.picker, true);
    for(int k = 0; k < C.n_ids; k++)
        if(!strcmp(C.ids[k], "qwen3-vl")) lv_dropdown_set_selected(C.dd, (uint32_t)k);
    drop_focus();
}

static void attach_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    bool show = lv_obj_is_hidden(C.picker);
    lv_obj_set_hidden(C.picker, !show);
    if(show) scan_files();
    drop_focus();
}

static void do_send(void)
{
    if(C.waiting) {
        net_chat_cancel();
        return;
    }
    const char *t = lv_textarea_get_text(C.ta);
    if((!t[0] && !C.image_path[0]) || C.n_ids == 0) return;
    const char *question = t[0] ? t : "Describe this image.";
    add_msg(true, question);
    lv_textarea_set_text(C.ta, "");
    const char *roles[MAXMSG];
    const char *texts[MAXMSG];
    int k = 0;
    int first = C.n > 12 ? C.n - 12 : 0;
    for(int i = first; i < C.n; i++) {
        roles[k] = C.m[i].user ? "user" : "assistant";
        texts[k++] = C.m[i].text;
    }
    uint32_t sel = lv_dropdown_get_selected(C.dd);
    if(net_chat_send(C.ids[sel < (uint32_t)C.n_ids ? sel : 0], k, roles, texts, MAX_TOKENS, C.image_path)) {
        C.image_path[0] = '\0';
        lv_label_set_text(C.attach_name, "");
        add_msg(false, "...");
        set_waiting(true);
        drop_focus();
        lv_label_set_text(C.stats, "");
        C.seen_gen = 0;
    }
}

static void send_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    do_send();
}

static void new_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if(C.waiting) net_chat_cancel();
    for(int i = 0; i < C.n; i++) free(C.m[i].text);
    lv_obj_clean(C.log);
    C.n = 0;
    C.image_path[0] = '\0';
    set_waiting(false);
    lv_label_set_text(C.stats, "");
    lv_label_set_text(C.attach_name, "");
    lv_obj_set_hidden(C.picker, true);
}

static void kb_set(bool on)
{
    lv_obj_set_hidden(C.kb, !on);
    lv_obj_update_layout(C.log);
    scroll_end();
}

/* The keyboard exists only while the text area has the focus: send, Esc, a tap elsewhere and a page change all drop it. */
static void drop_focus(void)
{
    kb_set(false);
    lv_group_focus_obj(app_key_sink());
}

static void log_tap_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    drop_focus();
}

static void ta_cb(lv_event_t *e)
{
    lv_event_code_t c = lv_event_get_code(e);
    if(c == LV_EVENT_READY) do_send();
    else if(c == LV_EVENT_FOCUSED) kb_set(true);
    else if(c == LV_EVENT_DEFOCUSED) kb_set(false);
    else if(c == LV_EVENT_KEY && lv_event_get_key(e) == LV_KEY_ESC) drop_focus();
}

static void kb_cb(lv_event_t *e)
{
    lv_event_code_t c = lv_event_get_code(e);
    if(c == LV_EVENT_READY) do_send();
    else if(c == LV_EVENT_CANCEL) kb_set(false);
}

static lv_obj_t *button(lv_obj_t *parent, const char *text, bool primary, int32_t w, lv_obj_t **lbl)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, w, 42);
    lv_obj_set_style_bg_color(b, cb_col(primary ? CB_CYAN : CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(b, 8, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_color(b, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(b, primary ? 0 : 1, 0);
    lv_obj_t *l = ui_label(b, text, CB_F_VALUE, primary ? CB_BASE : CB_TEXT_HI);
    lv_obj_center(l);
    if(lbl) *lbl = l;
    return b;
}

static void build(lv_obj_t *page)
{
    lv_obj_set_scroll_dir(page, LV_DIR_NONE);
    lv_obj_t *top = ui_row(page, 10);
    ui_label(top, "Model", CB_F_BODY, CB_TEXT_MID);
    C.dd = lv_dropdown_create(top);
    lv_dropdown_set_options(C.dd, "loading models...");
    lv_obj_set_width(C.dd, 300);
    lv_obj_set_style_text_font(C.dd, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_bg_color(C.dd, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(C.dd, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(C.dd, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_border_color(C.dd, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(C.dd, 1, 0);
    lv_obj_set_style_radius(C.dd, 8, 0);
    lv_obj_set_style_text_font(C.dd, &lv_font_montserrat_14, LV_PART_INDICATOR);
    lv_obj_set_style_text_color(C.dd, cb_col(CB_TEXT_MID), LV_PART_INDICATOR);
    lv_obj_t *list = lv_dropdown_get_list(C.dd);
    lv_obj_set_style_bg_color(list, cb_col(CB_SURFACE_HI), 0);
    lv_obj_set_style_bg_opa(list, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(list, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(list, cb_font(CB_F_BODY), 0);
    lv_obj_set_style_border_color(list, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_bg_color(list, cb_col(CB_CYAN), LV_PART_SELECTED | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(list, cb_col(CB_BASE), LV_PART_SELECTED | LV_STATE_CHECKED);
    C.state = ui_label(top, "", CB_F_BODY, CB_TEXT_MID);
    ui_spacer(top);
    lv_obj_t *nb = button(top, "New chat", false, 120, NULL);
    lv_obj_add_event_cb(nb, new_cb, LV_EVENT_CLICKED, NULL);

    C.log = ui_plain(page);
    lv_obj_set_width(C.log, lv_pct(100));
    lv_obj_set_flex_grow(C.log, 1);
    lv_obj_set_flex_flow(C.log, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(C.log, 10, 0);
    lv_obj_set_scrollable(C.log, true);
    lv_obj_set_scroll_dir(C.log, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(C.log, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_clickable(C.log, true);
    lv_obj_add_event_cb(C.log, log_tap_cb, LV_EVENT_CLICKED, NULL);

    C.stats = ui_label(page, "", CB_F_BODY, CB_TEXT_MID);
    C.attach_name = ui_label(page, "", CB_F_BODY, CB_CYAN);

    C.picker = ui_card(page);
    ui_label(C.picker, "Choose an image from ~/Pictures (JPEG or PNG, up to 4 MB)", CB_F_BODY, CB_TEXT_MID);
    for(int i = 0; i < PICKER_MAX; i++) {
        C.file_btn[i] = button(C.picker, "", false, 320, NULL);
        lv_obj_add_event_cb(C.file_btn[i], file_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        lv_obj_set_hidden(C.file_btn[i], true);
    }
    lv_obj_set_hidden(C.picker, true);

    lv_obj_t *in = ui_row(page, 10);
    lv_obj_t *attach = button(in, "", false, 48, NULL);
    lv_obj_t *attach_icon = ui_icon(attach, UI_ICON_IMAGE, CB_TEXT_HI);
    lv_obj_center(attach_icon);
    lv_obj_add_event_cb(attach, attach_cb, LV_EVENT_CLICKED, NULL);
    C.ta = lv_textarea_create(in);
    lv_textarea_set_one_line(C.ta, true);
    lv_textarea_set_placeholder_text(C.ta, "Ask the box anything...");
    lv_obj_set_height(C.ta, 46);
    lv_obj_set_flex_grow(C.ta, 1);
    lv_obj_set_style_bg_color(C.ta, cb_col(CB_SURFACE), 0);
    lv_obj_set_style_bg_opa(C.ta, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(C.ta, cb_col(CB_TEXT_HI), 0);
    lv_obj_set_style_text_font(C.ta, cb_font(CB_F_VALUE), 0);
    lv_obj_set_style_border_color(C.ta, cb_col(CB_HAIRLINE), 0);
    lv_obj_set_style_border_width(C.ta, 1, 0);
    lv_obj_set_style_border_color(C.ta, cb_col(CB_CYAN), LV_STATE_FOCUSED);
    lv_obj_set_style_radius(C.ta, 8, 0);
    lv_obj_set_style_text_color(C.ta, cb_col(CB_TEXT_LO), LV_PART_TEXTAREA_PLACEHOLDER);
    lv_obj_set_style_bg_color(C.ta, cb_col(CB_CYAN), LV_PART_CURSOR);
    lv_obj_set_style_bg_opa(C.ta, LV_OPA_COVER, LV_PART_CURSOR);
    lv_obj_set_style_border_width(C.ta, 0, LV_PART_CURSOR);
    lv_obj_set_style_pad_ver(C.ta, 10, 0);
    lv_group_add_obj(cb_display_key_group(), C.ta);
    lv_obj_add_event_cb(C.ta, ta_cb, LV_EVENT_ALL, NULL);
    C.send = button(in, "Send", true, 110, &C.send_lbl);
    lv_obj_add_event_cb(C.send, send_cb, LV_EVENT_CLICKED, NULL);

    C.kb = lv_keyboard_create(page);
    lv_keyboard_set_textarea(C.kb, C.ta);
    lv_obj_set_size(C.kb, lv_pct(100), 270);
    lv_obj_set_style_bg_color(C.kb, cb_col(CB_SURFACE), 0);
    lv_obj_set_style_bg_opa(C.kb, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(C.kb, 0, 0);
    lv_obj_set_style_text_font(C.kb, &lv_font_montserrat_20, LV_PART_ITEMS);
    lv_obj_set_style_text_color(C.kb, cb_col(CB_TEXT_HI), LV_PART_ITEMS);
    lv_obj_set_style_bg_color(C.kb, cb_col(CB_SURFACE_HI), LV_PART_ITEMS);
    lv_obj_set_style_bg_opa(C.kb, LV_OPA_COVER, LV_PART_ITEMS);
    lv_obj_set_style_border_color(C.kb, cb_col(CB_HAIRLINE), LV_PART_ITEMS);
    lv_obj_set_style_border_width(C.kb, 1, LV_PART_ITEMS);
    lv_obj_set_style_bg_color(C.kb, cb_col(CB_HAIRLINE), LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_text_color(C.kb, cb_col(CB_CYAN), LV_PART_ITEMS | LV_STATE_CHECKED);
    lv_obj_set_style_bg_color(C.kb, cb_col(CB_CYAN), LV_PART_ITEMS | LV_STATE_PRESSED);
    lv_obj_set_style_text_color(C.kb, cb_col(CB_BASE), LV_PART_ITEMS | LV_STATE_PRESSED);
    lv_obj_add_event_cb(C.kb, kb_cb, LV_EVENT_ALL, NULL);
    lv_obj_set_hidden(C.kb, true);
}

static void fill_models(const net_snapshot_t *s)
{
    char opts[NET_MAX_MODELS * 42];
    opts[0] = '\0';
    char prev[40] = "";
    if(C.n_ids > 0) snprintf(prev, sizeof prev, "%s", C.ids[lv_dropdown_get_selected(C.dd) < (uint32_t)C.n_ids ? lv_dropdown_get_selected(C.dd) : 0]);
    C.n_ids = 0;
    int pick = 0;
    for(int i = 0; i < s->n_models; i++) {
        const net_model_t *m = &s->models[i];
        if(strcmp(m->modality, "chat") && strcmp(m->modality, "vlm")) continue;
        snprintf(C.ids[C.n_ids], sizeof C.ids[0], "%s", m->id);
        if(!strcmp(m->id, prev[0] ? prev : "qwen3-vl")) pick = C.n_ids;
        if(C.n_ids) strcat(opts, "\n");
        strcat(opts, m->id);
        C.n_ids++;
    }
    if(C.n_ids == 0) return;
    lv_dropdown_set_options(C.dd, opts);
    lv_dropdown_set_selected(C.dd, (uint32_t)pick);
}

static void poll_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    static net_chat_t c;
    net_chat_take(&c);
    if(!C.waiting || c.gen == C.seen_gen || C.n == 0) return;
    C.seen_gen = c.gen;
    msg_t *m = &C.m[C.n - 1];
    if(c.error) {
        char b[NET_CHAT_CAP + 200];
        snprintf(b, sizeof b, "%s%s%s", c.text, c.text[0] ? "\n\n" : "", c.err);
        lv_label_set_text(m->body, b);
        lv_obj_set_style_text_color(m->body, cb_col(CB_RED), 0);
        lv_label_set_text(C.stats, "Request failed");
    }
    else lv_label_set_text(m->body, c.text[0] ? c.text : "...");
    if(c.note[0]) lv_label_set_text(C.state, c.note);
    if(!c.streaming) {
        free(m->text);
        m->text = strdup(c.error ? "" : c.text);
        if(!c.error) {
            char b[120];
            snprintf(b, sizeof b, "TTFT %.0f ms  \xC2\xB7  %.1f tokens/s  \xC2\xB7  %d tokens", c.ttft_ms, c.tps, c.tokens);
            lv_label_set_text(C.stats, b);
        }
        lv_label_set_text(C.state, "");
        set_waiting(false);
    }
    scroll_end();
}

static void tick(const net_snapshot_t *s)
{
    if(C.seen_models != s->models_seq) {
        C.seen_models = s->models_seq;
        fill_models(s);
    }
}

static const char *s_demo;

static void demo_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    if(C.n_ids == 0) return;
    lv_textarea_set_text(C.ta, s_demo);
    do_send();
    s_demo = NULL;
}

void chat_demo(const char *text)
{
    s_demo = text;
}

static bool s_demo_focus;

void chat_demo_focus(void)
{
    s_demo_focus = true;
}

static void focus_cb(lv_timer_t *t)
{
    LV_UNUSED(t);
    lv_group_focus_obj(C.ta);   /* what a tap on the field does */
}

static void show(bool on)
{
    if(on) {
        drop_focus();
        lv_obj_set_hidden(C.picker, true);
    }
    if(on && s_demo_focus) lv_timer_set_repeat_count(lv_timer_create(focus_cb, 3000, NULL), 1);
    if(on && s_demo) lv_timer_set_repeat_count(lv_timer_create(demo_cb, 1200, NULL), 1);
    if(on && !C.timer) C.timer = lv_timer_create(poll_cb, 50, NULL);
    if(!on) drop_focus();
}

const app_page_t page_chat = {
    .nav = "Chat & VLM", .icon = UI_ICON_CHAT, .eyebrow = "EDGE AI / LANGUAGE", .title = "Chat & VLM",
    .subtitle = "Chat with the models on the Hailo-10H, streamed token by token.",
    .build = build, .tick = tick, .show = show,
};
