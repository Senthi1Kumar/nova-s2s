#pragma once
#include "lvgl.h"

/* Nova settings: the language model behind Nova's answers (provider, model, automatic failover between providers),
   read from and written to the pipeline's /v1/settings over the live link. The sheet opens over everything; Apply
   or Cancel closes it. settings_sheet_init() keeps the status-strip chip in step with nova.settings frames. */
void settings_sheet_init(lv_obj_t *chip);
void settings_sheet_open(void);
