#pragma once
#include "live/live.h"
#include "lvgl.h"

/* Nova's settings, built into a column of the Nova page: the language model (provider, model, automatic failover),
   the voice (voice, speed, preview) and the avatar head. They read and write the pipeline's /v1/settings over the
   live link and follow nova.settings frames; without a link every control is off. */
void settings_sheet_build(lv_obj_t *parent);

/* For tests only: a link without a pipeline. In test mode jobs are recorded and wait for _finish() instead of
   running on a worker. kind: 0 fetch, 1 language model, 2 voice, 3 preview. */
void settings_sheet_test_mode(bool on);
int settings_sheet_test_jobs(void);
bool settings_sheet_test_busy(void);
int settings_sheet_test_kind(void);
void settings_sheet_test_sent(char *voice48, char *speed8, char *avatar12);
void settings_sheet_test_finish(bool ok, const char *err, const live_llm_t *llm, const live_tts_t *tts);
void settings_sheet_test_forget(bool llm, bool tts);   /* back to "settings not known yet" */
