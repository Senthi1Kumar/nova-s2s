#include "widgets/connected.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "widgets/ui_kit.h"

#define DOT "\xC2\xB7"
#define MAX_ROWS 3

/* ---------- last turn ---------- */

static lv_obj_t *s_utter, *s_router, *s_latency;
static lv_obj_t *s_call_row[MAX_ROWS], *s_call_api[MAX_ROWS], *s_call_args[MAX_ROWS], *s_call_verdict[MAX_ROWS];
static lv_obj_t *s_diffs, *s_diff_row[MAX_ROWS], *s_diff_path[MAX_ROWS], *s_diff_change[MAX_ROWS];

static void verdict_chip(lv_obj_t *chip, cb_verdict_t v)
{
    static const char *const text[] = { "execute", "confirm_first", "decline" };
    static const cb_color_t color[] = { CB_GREEN, CB_YELLOW, CB_ORANGE };
    int i = v <= CB_VERDICT_DECLINE ? (int)v : (int)CB_VERDICT_DECLINE;
    lv_label_set_text(ui_chip_label(chip), text[i]);
    lv_obj_set_style_bg_color(lv_obj_get_child(chip, 0), cb_col(color[i]), 0);
}

static void last_turn_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    const cb_last_turn_t *t = lv_subject_get_pointer(s);
    if(!t) return;
    lv_label_set_text_fmt(s_utter, "\xE2\x80\x9C%s\xE2\x80\x9D", t->utterance);
    lv_label_set_text_fmt(ui_chip_label(s_router), "router " DOT " %s", t->router);
    lv_label_set_text(ui_chip_label(s_latency), t->latency);
    for(int i = 0; i < MAX_ROWS; i++) {
        bool on = i < t->n_calls;
        lv_obj_set_hidden(s_call_row[i], !on);
        if(!on) continue;
        lv_label_set_text(s_call_api[i], t->calls[i].api);
        lv_label_set_text(s_call_args[i], t->calls[i].args);
        verdict_chip(s_call_verdict[i], t->calls[i].verdict);
    }
    lv_obj_set_hidden(s_diffs, t->n_diffs <= 0);
    for(int i = 0; i < MAX_ROWS; i++) {
        bool on = i < t->n_diffs;
        lv_obj_set_hidden(s_diff_row[i], !on);
        if(!on) continue;
        lv_label_set_text(s_diff_path[i], t->diffs[i].path);
        lv_label_set_text(s_diff_change[i], t->diffs[i].change);
    }
}

static lv_obj_t *section_label(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_add_style(l, &cb_st_label, 0);
    lv_label_set_text(l, text);
    return l;
}

static void last_turn_card(lv_obj_t *col)
{
    lv_obj_t *card = ui_card(col);
    section_label(card, "LAST TURN");
    s_utter = ui_label_line(card, "", CB_F_BODY, CB_TEXT_HI);

    lv_obj_t *chips = ui_row(card, 6);
    s_router = ui_chip(chips, "router", CB_VIOLET, false);
    s_latency = ui_chip(chips, "", CB_TEXT_MID, false);

    for(int i = 0; i < MAX_ROWS; i++) {
        s_call_row[i] = ui_row(card, 8);
        s_call_api[i] = ui_label(s_call_row[i], "", CB_F_BODY, CB_TEXT_HI);
        s_call_args[i] = ui_label_line(s_call_row[i], "", CB_F_LABEL, CB_TEXT_MID);
        lv_obj_set_flex_grow(s_call_args[i], 1);
        s_call_verdict[i] = ui_chip(s_call_row[i], "", CB_GREEN, false);
    }

    s_diffs = ui_col(card, 2);
    for(int i = 0; i < MAX_ROWS; i++) {
        s_diff_row[i] = ui_row(s_diffs, 10);
        s_diff_path[i] = ui_label_line(s_diff_row[i], "", CB_F_MONO_S, CB_TEXT_LO);
        lv_obj_set_flex_grow(s_diff_path[i], 1);
        s_diff_change[i] = ui_label(s_diff_row[i], "", CB_F_MONO_S, CB_CYAN);
    }
    lv_subject_add_observer_obj(cb_model()->last_turn, last_turn_obs, card, NULL);
}

/* ---------- tools & MCP ---------- */

static void tools_card(lv_obj_t *col)
{
    static const struct { const char *name, *status; cb_color_t c; } k_tools[] = {
        { "Web search", "Exa " DOT " ready", CB_GREEN },
        { "Deep research", "idle", CB_TEXT_MID },
        { "Google Workspace", "Gmail " DOT " Calendar", CB_GREEN },
        { "MCP connectors", "3 enabled " DOT " 21 tools", CB_CYAN },
        { "Vehicle (CabinGym)", "12 devices " DOT " 57 APIs", CB_YELLOW },
    };
    lv_obj_t *card = ui_card(col);
    section_label(card, "TOOLS & MCP");
    lv_obj_t *list = ui_col(card, 8);
    for(size_t i = 0; i < sizeof(k_tools) / sizeof(k_tools[0]); i++) {
        lv_obj_t *row = ui_row(list, 10);
        ui_dot(row, k_tools[i].c, k_tools[i].c == CB_GREEN);
        ui_label(row, k_tools[i].name, CB_F_BODY, CB_TEXT_HI);
        ui_spacer(row);
        ui_label(row, k_tools[i].status, CB_F_LABEL, CB_TEXT_MID);
    }
}

/* ---------- phone ---------- */

static lv_obj_t *s_call_on, *s_call_off, *s_mute, *s_end;
static bool s_muted;

static void call_active_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    bool on = lv_subject_get_int(s) != 0;
    lv_obj_set_hidden(s_call_on, !on);
    lv_obj_set_hidden(s_call_off, on);
    lv_obj_set_hidden(s_mute, !on);
    lv_obj_set_hidden(s_end, !on);
    if(!on && s_muted) {
        s_muted = false;
        lv_label_set_text(lv_obj_get_child(s_mute, 0), "Mute");
    }
}

static void call_s_obs(lv_observer_t *o, lv_subject_t *s)
{
    LV_UNUSED(o);
    int32_t t = lv_subject_get_int(s);
    lv_label_set_text_fmt(ui_chip_label(s_call_on), "Calling Sam " DOT " %d:%02d", (int)(t / 60), (int)(t % 60));
}

static void mute_click(lv_event_t *e)
{
    LV_UNUSED(e);
    s_muted = !s_muted;
    lv_label_set_text(lv_obj_get_child(s_mute, 0), s_muted ? "Unmute" : "Mute");
}

static void end_click(lv_event_t *e)
{
    LV_UNUSED(e);
    cb_model_set_int(cb_model()->call_active, 0);
}

static lv_obj_t *pill(lv_obj_t *parent, const char *text, bool red, lv_event_cb_t cb)
{
    lv_obj_t *p = ui_plain(parent);
    lv_obj_add_style(p, &cb_st_chip, 0);
    lv_obj_set_size(p, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_ver(p, 5, 0);
    lv_obj_set_style_pad_hor(p, 12, 0);
    lv_obj_set_clickable(p, true);
    lv_obj_set_ext_click_area(p, 6);
    if(red) {
        lv_obj_set_style_bg_color(p, cb_col(CB_RED), 0);
        lv_obj_set_style_bg_opa(p, 230, 0);
        lv_obj_set_style_text_color(p, lv_color_white(), 0);
    }
    lv_obj_set_style_bg_opa(p, red ? LV_OPA_COVER : 40, LV_STATE_PRESSED);
    lv_label_set_text(lv_label_create(p), text);
    lv_obj_add_event_cb(p, cb, LV_EVENT_CLICKED, NULL);
    return p;
}

static void phone_card(lv_obj_t *col)
{
    cb_model_t *m = cb_model();
    lv_obj_t *card = ui_card(col);

    lv_obj_t *head = ui_row(card, 6);
    ui_label(head, "Phone", CB_F_BODY, CB_TEXT_MID);
    ui_spacer(head);
    s_call_on = ui_chip(head, "Calling Sam", CB_GREEN, true);
    s_call_off = ui_chip(head, "No active call", CB_TEXT_MID, false);

    lv_obj_t *row = ui_row(card, 8);
    lv_obj_t *unread = ui_label_line(row, "2 unread from Priya " DOT " 1 missed (Alex)", CB_F_LABEL, CB_TEXT_2);
    lv_obj_set_flex_grow(unread, 1);
    s_mute = pill(row, "Mute", false, mute_click);
    s_end = pill(row, "End", true, end_click);

    lv_subject_add_observer_obj(m->call_s, call_s_obs, s_call_on, NULL);
    lv_subject_add_observer_obj(m->call_active, call_active_obs, card, NULL);
}

void connected_build(lv_obj_t *column) { connected_build_parts(column, column, column); }

void connected_build_parts(lv_obj_t *turn, lv_obj_t *tools, lv_obj_t *phone)
{
    last_turn_card(turn);
    tools_card(tools);
    phone_card(phone);
}
