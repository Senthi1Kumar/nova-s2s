#pragma once
#include <stdbool.h>

/* Live mode: Nova bar taps arrive as NV_FX_REQ_* bits and become POST /v1/session/{mute,arm,cancel}, run one at a
   time off the UI thread (a tap during a request queues, latest wins). The bar's buttons dim while one is in
   flight; a refusal shows a toast; success shows up as the nova.mic / nova.fsm frames that follow. */
void session_ctl_request(unsigned fx);
bool session_ctl_busy(void);
