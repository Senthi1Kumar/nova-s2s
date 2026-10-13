#pragma once
#include "lvgl.h"

/* Connected column contents (frame D): Last turn card (utterance, router/latency chips, tool calls with
   verdicts, VSS diffs) from the last_turn subject; Tools & MCP status list; Phone card (call chip with a
   live timer, unread line, Mute and End). */
void connected_build(lv_obj_t *column);
void connected_build_parts(lv_obj_t *turn, lv_obj_t *tools, lv_obj_t *phone);   /* layout B: cards in separate parents */
