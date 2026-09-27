/* Nova face: renders the UI-state contract (docs/api/ui-state.md) with LVGL v9.
 *
 * Layout and palette follow skills/nova-lvgl-ui/references/nova-face.md. The
 * setters are safe before nova_face_create (they do nothing), so a client can
 * start sending state before the screen exists. */
#include "nova_face.h"

#include <stdbool.h>
#include <string.h>

#define C_BG     0x0B0F14
#define C_TEXT   0xE6EDF3
#define C_MUTED  0x8B949E
#define C_IDLE   0x3A4A5C
#define C_LISTEN 0x2F81F7
#define C_THINK  0xA371F7
#define C_SPEAK  0x3FB950
#define C_WARN   0xD29922
#define C_ERROR  0xF85149

static struct {
    lv_obj_t *orb;    /* state colour; follows the mic level while listening */
    lv_obj_t *arc;    /* spinner while thinking */
    lv_obj_t *ring;   /* ripple while speaking */
    lv_obj_t *dot;    /* connection */
    lv_obj_t *you;
    lv_obj_t *nova;
    lv_obj_t *banner;
    int32_t orb_size;
    nova_state_t state;
    nova_face_wake_cb on_wake;
    void *user;
} F;

static uint32_t state_color(nova_state_t s)
{
    switch (s) {
    case NOVA_LISTENING: return C_LISTEN;
    case NOVA_THINKING:  return C_THINK;
    case NOVA_SPEAKING:  return C_SPEAK;
    default:             return C_IDLE;
    }
}

static lv_obj_t *circle(lv_obj_t *parent, int32_t size)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, size, size);
    lv_obj_set_style_radius(o, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_clickable(o, false);
    lv_obj_set_scrollable(o, false);
    lv_obj_center(o);
    return o;
}

static void anim_opa(void *obj, int32_t v) { lv_obj_set_style_bg_opa(obj, (lv_opa_t)v, 0); }
static void anim_spin(void *obj, int32_t v) { lv_arc_set_rotation(obj, v); }

static void anim_ripple(void *obj, int32_t v)
{
    int32_t size = F.orb_size + F.orb_size * v / 200;   /* orb size -> 1.5x */
    lv_obj_set_size(obj, size, size);
    lv_obj_set_style_border_opa(obj, (lv_opa_t)(255 - v * 255 / 100), 0);
}

static void loop_anim(void *var, lv_anim_exec_xcb_t cb, int32_t from, int32_t to,
                      uint32_t ms, bool reverse)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, var);
    lv_anim_set_exec_cb(&a, cb);
    lv_anim_set_values(&a, from, to);
    lv_anim_set_duration(&a, ms);
    if (reverse) {
        lv_anim_set_reverse_duration(&a, ms);
        lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
    }
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&a);
}

static const char *error_text(const char *type)
{
    if (strcmp(type, "session_limit_reached") == 0) return "Another screen is using Nova";
    if (strcmp(type, "deny_bad_token") == 0) return "Wrong API token";
    if (strcmp(type, "deny_no_token_configured") == 0) return "Nova needs an API token";
    return type;
}

static void on_button(lv_event_t *e)
{
    (void)e;
    nova_face_press_wake();
}

void nova_face_create(lv_obj_t *parent, nova_face_wake_cb on_wake, void *user)
{
    memset(&F, 0, sizeof F);
    F.on_wake = on_wake;
    F.user = user;

    lv_obj_update_layout(parent);
    int32_t w = lv_obj_get_content_width(parent);
    int32_t h = lv_obj_get_content_height(parent);
    bool wide = w * 10 > h * 13;                        /* wider than 1.3:1 */
    int32_t side = w < h ? w : h;
    F.orb_size = side * 26 / 100;

    lv_obj_set_style_bg_color(parent, lv_color_hex(C_BG), 0);
    lv_obj_set_style_bg_opa(parent, LV_OPA_COVER, 0);
    lv_obj_set_scrollable(parent, false);

    /* Stage: orb, ripple and spinner, sized for the largest ripple. */
    lv_obj_t *stage = lv_obj_create(parent);
    lv_obj_remove_style_all(stage);
    lv_obj_set_size(stage, F.orb_size * 16 / 10, F.orb_size * 16 / 10);
    lv_obj_set_clickable(stage, false);
    lv_obj_set_scrollable(stage, false);
    if (wide)
        lv_obj_align(stage, LV_ALIGN_LEFT_MID, w / 10, 0);
    else
        lv_obj_align(stage, LV_ALIGN_CENTER, 0, -h / 8);

    F.ring = circle(stage, F.orb_size);
    lv_obj_set_style_border_width(F.ring, 3, 0);
    lv_obj_set_style_border_color(F.ring, lv_color_hex(C_SPEAK), 0);

    F.orb = circle(stage, F.orb_size);
    lv_obj_set_style_bg_opa(F.orb, LV_OPA_COVER, 0);

    F.arc = lv_arc_create(stage);
    lv_obj_set_size(F.arc, F.orb_size * 125 / 100, F.orb_size * 125 / 100);
    lv_obj_center(F.arc);
    lv_arc_set_bg_angles(F.arc, 0, 360);
    lv_arc_set_angles(F.arc, 0, 70);
    lv_obj_remove_style(F.arc, NULL, LV_PART_KNOB);
    lv_obj_set_clickable(F.arc, false);
    lv_obj_set_style_arc_opa(F.arc, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_arc_color(F.arc, lv_color_hex(C_THINK), LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(F.arc, 6, LV_PART_INDICATOR);

    /* Text column: subtitles over the tap-to-talk button. On square and round
     * panels it sits low at 60% width, inside the inscribed circle. */
    lv_obj_t *col = lv_obj_create(parent);
    lv_obj_remove_style_all(col);
    lv_obj_set_scrollable(col, false);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(col, side / 40, 0);
    if (wide) {
        lv_obj_set_size(col, w * 50 / 100, h * 60 / 100);
        lv_obj_align(col, LV_ALIGN_RIGHT_MID, -w / 20, 0);
    } else {
        lv_obj_set_width(col, w * 60 / 100);
        lv_obj_set_height(col, LV_SIZE_CONTENT);
        lv_obj_align(col, LV_ALIGN_BOTTOM_MID, 0, -h * 15 / 100);
    }

    const lv_font_t *font = side >= 400 ? &lv_font_montserrat_20 : &lv_font_montserrat_14;
    int32_t line_h = lv_font_get_line_height(font);

    F.you = lv_label_create(col);
    F.nova = lv_label_create(col);
    lv_obj_t *labels[2] = { F.you, F.nova };
    for (int i = 0; i < 2; i++) {
        lv_obj_set_width(labels[i], lv_pct(100));
        lv_label_set_long_mode(labels[i], LV_LABEL_LONG_MODE_DOTS);
        lv_obj_set_style_text_font(labels[i], font, 0);
        lv_obj_set_style_text_align(labels[i], LV_TEXT_ALIGN_CENTER, 0);
        lv_label_set_text(labels[i], "");
    }
    lv_obj_set_height(F.you, line_h);            /* one line, then "..." */
    lv_obj_set_height(F.nova, line_h * 2);       /* two lines, then "..." */
    lv_obj_set_style_text_color(F.you, lv_color_hex(C_MUTED), 0);
    lv_obj_set_style_text_color(F.nova, lv_color_hex(C_TEXT), 0);
    if (side < 300) lv_obj_set_hidden(F.you, true);   /* small panels: clear the orb */

    lv_obj_t *btn = lv_button_create(col);
    lv_obj_add_event_cb(btn, on_button, LV_EVENT_CLICKED, NULL);
    lv_obj_t *btn_label = lv_label_create(btn);
    lv_label_set_text(btn_label, "Tap to talk");
    lv_obj_set_style_text_font(btn_label, font, 0);

    /* Connection dot and error banner near the top centre, inside the circle. */
    F.dot = circle(parent, side / 30 > 8 ? side / 30 : 8);
    lv_obj_align(F.dot, LV_ALIGN_TOP_MID, 0, h * 8 / 100);
    lv_obj_set_style_bg_opa(F.dot, LV_OPA_COVER, 0);

    F.banner = lv_label_create(parent);
    lv_obj_set_width(F.banner, w * 50 / 100);
    lv_label_set_long_mode(F.banner, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_align(F.banner, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(F.banner, font, 0);
    lv_obj_set_style_text_color(F.banner, lv_color_hex(C_TEXT), 0);
    lv_obj_set_style_bg_color(F.banner, lv_color_hex(C_ERROR), 0);
    lv_obj_set_style_bg_opa(F.banner, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(F.banner, 6, 0);
    lv_obj_set_style_pad_top(F.banner, 4, 0);
    lv_obj_set_style_pad_bottom(F.banner, 4, 0);
    lv_obj_align(F.banner, LV_ALIGN_TOP_MID, 0, h * 12 / 100);
    lv_obj_set_hidden(F.banner, true);

    nova_face_set_conn(NOVA_CONN_CONNECTING);
    nova_face_set_state(NOVA_IDLE);
}

void nova_face_set_conn(nova_conn_t conn)
{
    if (!F.dot) return;
    uint32_t c = conn == NOVA_CONN_UP ? C_SPEAK : conn == NOVA_CONN_DOWN ? C_ERROR : C_WARN;
    lv_obj_set_style_bg_color(F.dot, lv_color_hex(c), 0);
    if (conn == NOVA_CONN_DOWN) nova_face_set_state(NOVA_IDLE);   /* stale state after disconnect */
}

void nova_face_set_state(nova_state_t s)
{
    if (!F.orb) return;
    F.state = s;
    lv_anim_delete(F.orb, NULL);
    lv_anim_delete(F.arc, NULL);
    lv_anim_delete(F.ring, NULL);
    lv_obj_set_size(F.orb, F.orb_size, F.orb_size);
    lv_obj_set_style_bg_color(F.orb, lv_color_hex(state_color(s)), 0);
    lv_obj_set_style_bg_opa(F.orb, LV_OPA_COVER, 0);
    lv_obj_set_hidden(F.arc, true);
    lv_obj_set_hidden(F.ring, true);
    switch (s) {
    case NOVA_IDLE:
        loop_anim(F.orb, anim_opa, 90, 180, 2000, true);          /* slow breathing */
        break;
    case NOVA_LISTENING:
        break;                                                     /* follows set_level */
    case NOVA_THINKING:
        lv_obj_set_hidden(F.arc, false);
        loop_anim(F.arc, anim_spin, 0, 360, 1000, false);
        break;
    case NOVA_SPEAKING:
        lv_obj_set_hidden(F.ring, false);
        loop_anim(F.ring, anim_ripple, 0, 100, 900, false);
        break;
    }
}

void nova_face_set_you(const char *text)
{
    if (!F.you) return;
    lv_label_set_text_fmt(F.you, "You: %s", text ? text : "");
}

void nova_face_set_nova(const char *text)
{
    if (!F.nova) return;
    lv_label_set_text(F.nova, text ? text : "");
}

void nova_face_set_level(float level)
{
    if (!F.orb || F.state != NOVA_LISTENING) return;
    if (!(level >= 0.0f)) level = 0.0f;          /* also catches NaN */
    if (level > 1.0f) level = 1.0f;
    int32_t size = F.orb_size + (int32_t)((float)F.orb_size * 0.25f * level);
    lv_obj_set_size(F.orb, size, size);
}

void nova_face_set_error(const char *type)
{
    if (!F.banner) return;
    if (!type || !*type) {
        lv_obj_set_hidden(F.banner, true);
        return;
    }
    lv_label_set_text(F.banner, error_text(type));
    lv_obj_set_hidden(F.banner, false);
}

void nova_face_press_wake(void)
{
    if (F.on_wake) F.on_wake(F.user);
}
