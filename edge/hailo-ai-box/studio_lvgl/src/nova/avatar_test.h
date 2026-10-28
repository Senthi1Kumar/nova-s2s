#pragma once
#include "nova/avatar.h"

/* Test-only views of an avatar: what is on screen and the model behind it. */
lv_color_t avatar_body_color(lv_obj_t *av);
lv_color_t avatar_eye_color(lv_obj_t *av);
const av_model_t *avatar_model(lv_obj_t *av);
av_head_t avatar_head(lv_obj_t *av);
