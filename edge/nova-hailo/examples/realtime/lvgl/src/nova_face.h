/* Nova face: the portable UI half of a Nova client with a screen.
 *
 * Renders the UI-state contract (docs/api/ui-state.md) with LVGL v9. No
 * platform code: a desktop shell feeds it JSON lines, a firmware calls these
 * functions from its realtime event handler. Call everything on the LVGL
 * thread. */
#pragma once

#include "lvgl.h"

typedef enum { NOVA_CONN_CONNECTING, NOVA_CONN_UP, NOVA_CONN_DOWN } nova_conn_t;
typedef enum { NOVA_IDLE, NOVA_LISTENING, NOVA_THINKING, NOVA_SPEAKING } nova_state_t;
typedef void (*nova_face_wake_cb)(void *user);

void nova_face_create(lv_obj_t *parent, nova_face_wake_cb on_wake, void *user);
void nova_face_set_conn(nova_conn_t conn);
void nova_face_set_state(nova_state_t state);
void nova_face_set_you(const char *text);
void nova_face_set_nova(const char *text);
void nova_face_set_level(float level);        /* 0..1, clamped */
void nova_face_set_error(const char *type);   /* NULL or "" clears */
void nova_face_press_wake(void);              /* same as a tap: hardware buttons, tests */
