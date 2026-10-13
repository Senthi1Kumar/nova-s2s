#include "cb_demo.h"
#include "cb_model.h"

/* Waits after AGENT_DONE, matched to the follow-up policy (nova_fsm): a conversation-ending turn waits
   window + settle + a pause at rest before the next "Hey Nova"; an answer-expecting turn hands over to its
   follow-up turn well inside the 8 s window, with no wake word. */
#define WAIT_DONE  5100u   /* 2000 window + 700 settle + 2400 at rest */
#define WAIT_ERROR 7100u   /* 4000 window + 700 settle + 2400 at rest */
#define WAIT_ASK   1600u

#define EV(e, ms)        { .kind = DS_EVENT, .ev = (e), .after_ms = (ms) }
#define EV_START(e, ms)  { .kind = DS_EVENT, .ev = (e), .after_ms = (ms), .turn_start = true }
#define DONE(ask, out, ms) { .kind = DS_EVENT, .ev = NV_EV_AGENT_DONE, .done = { (ask), (out) }, .after_ms = (ms) }
#define USER(t)          { .kind = DS_USER, .text = (t), .after_ms = 1100 }
#define NOVA(t)          { .kind = DS_NOVA, .text = (t), .after_ms = 2000 }
#define APPLY(id)        { .kind = DS_APPLY, .apply_id = (id) }

#define HEARD(user) \
    EV(NV_EV_USER_SPEAKING_ON, 0), USER(user), EV(NV_EV_USER_SPEAKING_OFF, 0), \
    EV(NV_EV_TURN_COMMITTED, 0), EV(NV_EV_AGENT_THINKING, 900)
#define WAKE_TURN(user)   EV_START(NV_EV_WAKE_KWS, 700), HEARD(user)
#define FOLLOW_TURN(user) { .kind = DS_EVENT, .ev = NV_EV_USER_SPEAKING_ON, .turn_start = true }, USER(user), \
    EV(NV_EV_USER_SPEAKING_OFF, 0), EV(NV_EV_TURN_COMMITTED, 0), EV(NV_EV_AGENT_THINKING, 900)
#define ANSWER(id, nova, ask, out, wait) APPLY(id), EV(NV_EV_AGENT_SPEAKING, 0), NOVA(nova), DONE(ask, out, wait)

const cb_step_t cb_demo_script[] = {
    WAKE_TURN("make it a bit warmer for me and call Sam"),
    ANSWER(APPLY_WARMER_CALL_SAM, "Driver side set to 21 degrees. Calling Sam.", false, NV_OUT_DONE, WAIT_DONE),

    WAKE_TURN("crack the rear windows"),
    ANSWER(APPLY_CRACK_REAR, "Rear windows open to 10 percent.", false, NV_OUT_DONE, WAIT_DONE),

    WAKE_TURN("turn on the fog lights"),
    ANSWER(APPLY_FOG_DECLINE, "Fog lights aren\xE2\x80\x99t connected in this car yet, so I can\xE2\x80\x99t switch them.",
           false, NV_OUT_ERROR, WAIT_ERROR),

    WAKE_TURN("find a coffee place near me"),
    ANSWER(APPLY_FIND_COFFEE, "Hilltop Cafe is 400 metres away, about 2 minutes. Want directions?",
           true, NV_OUT_DONE, WAIT_ASK),
    FOLLOW_TURN("yes, take me there"),
    ANSWER(APPLY_NAV_CAFE, "Starting directions to Hilltop Cafe.", false, NV_OUT_DONE, WAIT_DONE),

    WAKE_TURN("open the sunroof"),
    ANSWER(APPLY_SUNROOF_ASK, "You\xE2\x80\x99re driving at 62 km/h. Open the sunroof anyway?", true, NV_OUT_DONE, WAIT_ASK),
    FOLLOW_TURN("yes please"),
    ANSWER(APPLY_SUNROOF_OPEN, "Opening the sunroof.", false, NV_OUT_DONE, WAIT_DONE),
    { .kind = DS_APPLY, .apply_id = APPLY_RESET, .always = true },   /* loop housekeeping, Nova is at rest */
};
const size_t cb_demo_script_len = sizeof(cb_demo_script) / sizeof(cb_demo_script[0]);

#define ARROW "\xE2\x86\x92"
#define DOT "\xC2\xB7"

static const cb_last_turn_t k_warmer = {
    "make it a bit warmer for me and call Sam", "jev", "118 + 131 ms",
    { { "hvac " DOT " set_temperature", "driver +1 \xC2\xB0" "C", CB_VERDICT_EXECUTE },
      { "phone " DOT " call_contact", "Sam", CB_VERDICT_EXECUTE } }, 2,
    { { "HVAC\xE2\x80\xA6" "Driver.Temperature", "20.0 " ARROW " 21.0" },
      { "Nova.Phone.CallState", "idle " ARROW " dialing" } }, 2,
};
static const cb_last_turn_t k_crack = {
    "crack the rear windows", "regex", "0.4 ms",
    { { "windows " DOT " set_window", "zone=rear, value=10", CB_VERDICT_EXECUTE } }, 1,
    { { "Row2.*.Window.Position", "0 " ARROW " 10" } }, 1,
};
static const cb_last_turn_t k_fog = {
    "turn on the fog lights", "jev", "121 ms",
    { { "lighting " DOT " fog_light", "planned module", CB_VERDICT_DECLINE } }, 1,
    { { 0 } }, 0,
};
static const cb_last_turn_t k_coffee = {
    "find a coffee place near me", "jev", "Mapbox 212 ms",
    { { "navigation " DOT " find_nearby", "category=coffee", CB_VERDICT_EXECUTE } }, 1,
    { { 0 } }, 0,
};
static const cb_last_turn_t k_nav = {
    "yes, take me there", "jev", "Mapbox 188 ms",
    { { "navigation " DOT " navigate_to", "Hilltop Cafe", CB_VERDICT_EXECUTE } }, 1,
    { { "Nav.DestinationName", "home " ARROW " Hilltop Cafe" } }, 1,
};
static const cb_last_turn_t k_sunroof_ask = {
    "open the sunroof", "jev", "129 ms",
    { { "body " DOT " set_sunroof", "position=open", CB_VERDICT_CONFIRM } }, 1,
    { { 0 } }, 0,
};
static const cb_last_turn_t k_sunroof_open = {
    "yes please", "pending confirm", "0.1 ms",
    { { "body " DOT " set_sunroof", "position=open", CB_VERDICT_EXECUTE } }, 1,
    { { "Sunroof.Position", "15 " ARROW " 100" } }, 1,
};

void cb_demo_apply_to_model(int apply_id)
{
    cb_model_t *m = cb_model();
    switch(apply_id) {
        case APPLY_WARMER_CALL_SAM:
            cb_model_set_temp(true, 210);
            cb_model_set_int(m->call_s, 0);
            cb_model_set_int(m->call_active, 1);
            cb_model_set_last_turn(&k_warmer);
            break;
        case APPLY_CRACK_REAR:
            cb_model_set_window(CB_WIN_RL, 10);
            cb_model_set_window(CB_WIN_RR, 10);
            cb_model_set_last_turn(&k_crack);
            break;
        case APPLY_FOG_DECLINE:
            cb_model_set_last_turn(&k_fog);
            break;
        case APPLY_FIND_COFFEE:
            cb_model_set_last_turn(&k_coffee);
            break;
        case APPLY_NAV_CAFE:
            cb_model_set_last_turn(&k_nav);
            break;
        case APPLY_SUNROOF_ASK:
            cb_model_set_last_turn(&k_sunroof_ask);
            break;
        case APPLY_SUNROOF_OPEN:
            cb_model_set_sunroof(100);
            cb_model_set_last_turn(&k_sunroof_open);
            break;
        case APPLY_RESET:
            cb_model_set_temp(true, 200);
            for(int w = 0; w < CB_WIN_COUNT; w++) cb_model_set_window((cb_window_t)w, 0);
            cb_model_set_sunroof(15);
            break;
        default:
            break;
    }
}
