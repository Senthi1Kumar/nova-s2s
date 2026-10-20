#include "cb_app.h"
#include "cb_model.h"
#include "live/live.h"
#include "lvgl.h"
#include "nova/nova_fsm.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static int fails;
#define CHECK(c) do { if(!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while(0)

static void feed(const char *s) { live_feed(s, strlen(s)); }
static int32_t iv(lv_subject_t *s) { return lv_subject_get_int(s); }

static void polyline(void)
{
    double ll[8][2];
    int n = live_polyline6("wuglfAnp_~gF_rrAvdHg{\\vceB", ll, 8);
    CHECK(n == 3);
    CHECK(fabs(ll[0][1] - 37.4419) < 1e-9 && fabs(ll[0][0] + 122.143) < 1e-9);   /* lon, lat order */
    CHECK(fabs(ll[2][1] - 37.5) < 1e-9 && fabs(ll[2][0] + 122.2) < 1e-9);
    CHECK(live_polyline6("wuglfAnp_~gF_rrAvdHg{\\vceB", ll, 2) == 2);   /* capped */
    CHECK(live_polyline6("", ll, 8) == 0);
}

static void vehicle(void)
{
    cb_model_t *m = cb_model();
    feed("{\"type\":\"nova.vehicle\",\"seq\":0,\"adapter\":\"sim\",\"snapshot\":{"
         "\"Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature\":{\"value\":21.0,\"source\":\"sim\"},"
         "\"Vehicle.Cabin.HVAC.Station.Row1.Passenger.Temperature\":{\"value\":20.5,\"source\":\"sim\"},"
         "\"Vehicle.Speed\":{\"value\":47.6,\"source\":\"sim\"},"
         "\"Vehicle.Powertrain.Transmission.SelectedGear\":{\"value\":\"D\",\"source\":\"sim\"},"
         "\"Vehicle.Powertrain.TractionBattery.StateOfCharge.Displayed\":{\"value\":68.0,\"source\":\"sim\"},"
         "\"Vehicle.Powertrain.Range\":{\"value\":210.0,\"source\":\"sim\"},"
         "\"Vehicle.Cabin.Door.Row2.PassengerSide.Window.Position\":{\"value\":30,\"source\":\"sim\"},"
         "\"Vehicle.Cabin.Sunroof.Position\":{\"value\":100,\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Media.State\":{\"value\":\"playing\",\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Media.Title\":{\"value\":\"Raja\",\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Light.Ambient.Color\":{\"value\":\"red\",\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Light.Ambient.Intensity\":{\"value\":60,\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Lights.HeadlightMode\":{\"value\":\"low\",\"source\":\"sim\"},"
         "\"Vehicle.Cabin.HVAC.IsFrontDefrosterActive\":{\"value\":true,\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Mirrors.IsFolded\":{\"value\":true,\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.System.DriveMode\":{\"value\":\"sport\",\"source\":\"sim\"},"
         "\"Vehicle.Private.Nova.Wipers.Mode\":{\"value\":\"auto\",\"source\":\"sim\"},"
         "\"Vehicle.Unknown.Path\":{\"value\":1,\"source\":\"sim\"},"
         "\"Vehicle.Speed.Wrong\":{\"value\":\"fast\",\"source\":\"sim\"}}}");
    CHECK(iv(m->driver_temp_dc) == 210);
    CHECK(iv(m->passenger_temp_dc) == 205);
    CHECK(iv(m->speed_kph) == 48);
    CHECK(iv(m->gear) == CB_GEAR_D);
    CHECK(iv(m->soc_pct) == 68 && iv(m->range_km) == 210);
    CHECK(iv(m->win_pct[CB_WIN_RR]) == 30);
    CHECK(iv(m->sunroof_pct) == 100);
    CHECK(iv(m->media_playing) == 1);
    CHECK(strcmp(cb_model_media_title(), "Raja") == 0);
    CHECK(iv(m->ambient_color) == 2 && iv(m->ambient_level) == 60);
    CHECK(iv(m->headlights) == 2);
    CHECK(iv(m->defrost) == 1);
    CHECK(iv(m->mirrors_folded) == 1);
    CHECK(iv(m->drive_mode) == 2);
    CHECK(iv(m->wiper_mode) == 1 && iv(m->wipers_on) == 1);
    CHECK(live_seq() == 0);

    feed("{\"type\":\"nova.vehicle\",\"seq\":1,\"changed\":{\"Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature\":23.0}}");
    CHECK(iv(m->driver_temp_dc) == 230 && live_seq() == 1 && live_seq_gaps() == 0);
    feed("{\"type\":\"nova.vehicle\",\"seq\":1,\"changed\":{\"Vehicle.Speed\":10}}");   /* repeat: applied */
    CHECK(iv(m->speed_kph) == 10 && live_seq_gaps() == 0);
    feed("{\"type\":\"nova.vehicle\",\"seq\":3,\"changed\":{\"Vehicle.Speed\":20}}");   /* gap */
    CHECK(live_seq_gaps() == 1);
    feed("{\"type\":\"nova.vehicle\",\"seq\":4,\"changed\":{\"Vehicle.Speed\":\"oops\"}}");   /* wrong type ignored */
    feed("not json at all");
}

static void turn(void)
{
    feed("{\"type\":\"conversation.item.input_audio_transcription.completed\",\"transcript\":"
         "\"set the temperature to 23 degrees\",\"generation_id\":1}");
    feed("{\"type\":\"nova.agent_trace\",\"generation_id\":1,\"router\":\"jev\",\"latency_ms\":{\"route\":28.35},"
         "\"calls\":[{\"device\":\"hvac\",\"api\":\"set_temperature\",\"args\":{\"zone\":\"driver\",\"value\":23.0},"
         "\"gate\":{\"verdict\":\"execute\"},\"status\":\"success\"},"
         "{\"device\":\"body\",\"api\":\"set_sunroof\",\"args\":{\"position\":100},"
         "\"gate\":{\"verdict\":\"confirm_first\"},\"status\":\"pending\"}],"
         "\"diff\":[{\"path\":\"Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature\",\"before\":21.0,\"after\":23.0}],"
         "\"speak\":\"Driver side set to 23 degrees.\"}");
    const cb_last_turn_t *t = lv_subject_get_pointer(cb_model()->last_turn);
    CHECK(t != NULL);
    if(!t) return;
    CHECK(!strcmp(t->utterance, "set the temperature to 23 degrees"));
    CHECK(strstr(t->router, "jev") != NULL);
    CHECK(!strcmp(t->latency, "28 ms"));
    CHECK(t->n_calls == 2);
    CHECK(!strcmp(t->calls[0].api, "hvac \xC2\xB7 set_temperature"));
    CHECK(strstr(t->calls[0].args, "zone=driver") && strstr(t->calls[0].args, "value=23"));
    CHECK(t->calls[0].verdict == CB_VERDICT_EXECUTE && t->calls[1].verdict == CB_VERDICT_CONFIRM);
    CHECK(t->n_diffs == 1);
    CHECK(strstr(t->diffs[0].path, "Driver.Temperature") != NULL);
    CHECK(!strcmp(t->diffs[0].change, "21 \xE2\x86\x92 23"));
}

static void remote_fsm(void)
{
    nova_fsm_t f;
    nova_fsm_init(&f, NULL);
    unsigned fx = nova_fsm_remote(&f, "LISTENING", 0);
    CHECK(f.activity == NV_LISTENING && f.view == NV_VIEW_OPEN && (fx & NV_FX_ACTIVITY) && (fx & NV_FX_VIEW));
    nova_fsm_remote(&f, "TRANSCRIBING", 100);
    CHECK(f.activity == NV_THINKING);
    nova_fsm_remote(&f, "INTERRUPTING", 200);
    CHECK(f.activity == NV_SPEAKING);
    nova_fsm_remote(&f, "IDLE", 300);
    CHECK(f.activity == NV_IDLE && f.view == NV_VIEW_OPEN && f.settling);
    nova_fsm_handle(&f, NV_EV_TICK, NULL, 300 + f.cfg.settle_ms);
    CHECK(f.view == NV_VIEW_REST);
    CHECK(nova_fsm_handle(&f, NV_EV_TAP_MIC, NULL, 400) == NV_FX_REQ_MUTE && !f.muted);   /* asks; no fake mute */
    CHECK(nova_fsm_handle(&f, NV_EV_TAP_ORB, NULL, 400) == NV_FX_REQ_ARM && f.activity == NV_IDLE);
    CHECK(nova_fsm_handle(&f, NV_EV_TAP_TUCK, NULL, 400) == 0);   /* already resting: tuck is still allowed */
}

static void mic_frames(void)
{
    nova_fsm_t *f = cb_app_fsm();
    feed("{\"type\":\"nova.mic\",\"muted\":true,\"source\":\"cockpit\"}");
    CHECK(f->muted);
    feed("{\"type\":\"nova.mic\",\"muted\":false,\"source\":\"voice\"}");
    CHECK(!f->muted);
    feed("{\"type\":\"nova.mic\"}");   /* malformed: keeps the last known state */
    CHECK(!f->muted);
}

static const char *body_of(const char *req, size_t n)
{
    const char *b = strstr(req, "\r\n\r\n");
    if(!b || !n) return "";
    char cl[64];
    snprintf(cl, sizeof cl, "Content-Length: %zu\r\n", strlen(b + 4));
    return strstr(req, cl) ? b + 4 : "(bad length)";
}

static void session_requests(void)
{
    char req[512];
    size_t n = live_session_request(req, sizeof req, "127.0.0.1", 8766, LIVE_SESSION_MUTE, true);
    CHECK(!strncmp(req, "POST /v1/session/mute HTTP/1.1\r\n", strlen("POST /v1/session/mute HTTP/1.1\r\n")));
    CHECK(!strcmp(body_of(req, n), "{\"muted\":true,\"source\":\"cockpit\"}"));
    n = live_session_request(req, sizeof req, "127.0.0.1", 8766, LIVE_SESSION_MUTE, false);
    CHECK(!strcmp(body_of(req, n), "{\"muted\":false,\"source\":\"cockpit\"}"));
    n = live_session_request(req, sizeof req, "127.0.0.1", 8766, LIVE_SESSION_ARM, false);
    CHECK(!strncmp(req, "POST /v1/session/arm HTTP/1.1\r\n", strlen("POST /v1/session/arm HTTP/1.1\r\n")));
    CHECK(!strcmp(body_of(req, n), "{}"));
    n = live_session_request(req, sizeof req, "127.0.0.1", 8766, LIVE_SESSION_CANCEL, false);
    CHECK(!strncmp(req, "POST /v1/session/cancel HTTP/1.1\r\n", strlen("POST /v1/session/cancel HTTP/1.1\r\n")));
    CHECK(!strcmp(body_of(req, n), "{}"));
    CHECK(live_session_request(req, 16, "127.0.0.1", 8766, LIVE_SESSION_ARM, false) == 0);   /* too small */
}

static void set_request(void)
{
    char out[512];
    size_t n = live_set_request(out, sizeof out, "127.0.0.1", 8766,
                                "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", 19.5);
    CHECK(n > 0 && n < sizeof out);
    CHECK(!strncmp(out, "POST /v1/vehicle/set HTTP/1.1\r\n", 31));
    const char *body = strstr(out, "\r\n\r\n");
    CHECK(body != NULL);
    if(!body) return;
    body += 4;
    CHECK(!strcmp(body, "{\"path\":\"Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature\",\"value\":19.5}"));
    char cl[64];
    snprintf(cl, sizeof cl, "Content-Length: %zu\r\n", strlen(body));
    CHECK(strstr(out, cl) != NULL);
}

/* Writes the cockpit used to drop: a tilted sunroof showed closed, and the passenger
   fan and the sunshade never showed at all. */
static void cabin_writes_shown(void)
{
    cb_model_t *m = cb_model();
    feed("{\"type\":\"nova.vehicle\",\"seq\":100,\"snapshot\":{}}");   /* sequence starts here */
    feed("{\"type\":\"nova.vehicle\",\"seq\":101,\"changed\":{"
         "\"Vehicle.Cabin.Sunroof.Position\":-100,"
         "\"Vehicle.Cabin.Sunroof.Shade.Position\":100,"
         "\"Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed\":1,"
         "\"Vehicle.Cabin.HVAC.Station.Row1.Passenger.FanSpeed\":6}}");
    CHECK(iv(m->sunroof_pct) == -100);
    CHECK(iv(m->sunshade_pct) == 100);
    CHECK(iv(m->fan_level) == 6);   /* the stronger zone */
    feed("{\"type\":\"nova.vehicle\",\"seq\":102,\"changed\":{"
         "\"Vehicle.Cabin.HVAC.Station.Row1.Passenger.FanSpeed\":0}}");
    CHECK(iv(m->fan_level) == 1);
}

/* Results show as pins; choosing one leaves only the destination; a new search shows pins again. */
static void result_pins(void)
{
    const char *pois = "\"pois\":[{\"n\":1,\"name\":\"Taqueria La Cazuela\",\"lat\":37.4590,\"lon\":-122.1380},"
                       "{\"n\":2,\"name\":\"Compadres Food Truck\",\"lat\":37.4602,\"lon\":-122.1351},"
                       "{\"n\":3,\"name\":\"Ravenswood\",\"lat\":37.4605,\"lon\":-122.1350}]";
    char f[1024];
    snprintf(f, sizeof f, "{\"type\":\"nova.nav\",\"active\":false,\"route\":\"\",%s}", pois);
    feed(f);
    CHECK(live_pins_shown() == 3);
    snprintf(f, sizeof f, "{\"type\":\"nova.nav\",\"active\":true,\"route\":\"\",\"destination\":"
             "{\"name\":\"Taqueria La Cazuela\",\"lat\":37.4590,\"lon\":-122.1380},%s}", pois);
    feed(f);
    CHECK(live_pins_shown() == 0);   /* the chosen one is the destination now; the others go */
    feed("{\"type\":\"nova.nav\",\"active\":true,\"route\":\"\",\"destination\":"
         "{\"name\":\"Taqueria La Cazuela\",\"lat\":37.4590,\"lon\":-122.1380},"
         "\"pois\":[{\"n\":1,\"name\":\"Blue Bottle\",\"lat\":37.4470,\"lon\":-122.1600}]}");
    CHECK(live_pins_shown() == 1);   /* a new search on the way shows again */
    feed("{\"type\":\"nova.nav\",\"active\":false,\"route\":\"\","
         "\"pois\":[{\"n\":1,\"name\":\"Blue Bottle\",\"lat\":37.4470,\"lon\":-122.1600}]}");
    CHECK(live_pins_shown() == 0);   /* the trip ended: its results are done */
}

static void position_label(void)
{
    char t[80];
    CHECK(live_position_label(1, 7, "gps", false, t, sizeof t) && !strcmp(t, "GPS \xC2\xB7 7 sats"));
    CHECK(!live_position_label(0, 0, "gps", false, t, sizeof t));   /* fix lost: deltas carry no source */
    CHECK(!strcmp(t, "GPS \xC2\xB7 no fix \xC2\xB7 simulated drive"));
    CHECK(live_position_label(-1, 0, "gps", false, t, sizeof t) && !strcmp(t, "GPS \xC2\xB7 fix"));
    CHECK(!live_position_label(-1, 0, "sim", false, t, sizeof t) && strstr(t, "simulated drive"));
    CHECK(!live_position_label(1, 7, "gps", true, t, sizeof t));    /* a route plays: the drive owns the puck */
    CHECK(!strcmp(t, "Simulated drive \xC2\xB7 GPS 7 sats"));
}

static void timezone_follows_car(void)
{
    CHECK(live_set_timezone("America/Los_Angeles") && !strcmp(getenv("TZ"), "America/Los_Angeles"));
    time_t noon_utc = 1791892800;   /* 2026-10-13 12:00 UTC */
    struct tm tmv;
    localtime_r(&noon_utc, &tmv);
    CHECK(tmv.tm_hour == 5);   /* PDT */
    CHECK(live_set_timezone("Asia/Kolkata"));
    localtime_r(&noon_utc, &tmv);
    CHECK(tmv.tm_hour == 17 && tmv.tm_min == 30);
    CHECK(!live_set_timezone("") && !live_set_timezone("../etc/passwd") && !live_set_timezone("A B"));
    CHECK(!strcmp(getenv("TZ"), "Asia/Kolkata"));   /* a bad name leaves the clock alone */
}

static void llm_settings(void)
{
    const char *t = "{\"llm\":{\"provider\":\"openrouter\",\"model\":\"openai/gpt-oss-120b\",\"failover\":true,"
                    "\"failover_ready\":true,"
                    "\"providers\":[{\"id\":\"groq\",\"ok\":false,\"models\":[\"openai/gpt-oss-120b\",\"llama-3.3-70b\"]},"
                    "{\"id\":\"openrouter\",\"ok\":true,\"models\":[\"openai/gpt-oss-120b\",\"google/gemini-2.5-flash-lite\","
                    "\"qwen/qwen3-235b-a22b-2507\"]}]}}";
    live_llm_t l;
    CHECK(live_llm_parse_json(t, strlen(t), &l));
    CHECK(!strcmp(l.provider, "openrouter") && !strcmp(l.model, "openai/gpt-oss-120b") && l.failover);
    CHECK(l.failover_ready);
    CHECK(l.n == 2);
    CHECK(!strcmp(l.p[0].id, "groq") && !l.p[0].ok && l.p[0].n_models == 2);
    CHECK(!strcmp(l.p[1].id, "openrouter") && l.p[1].ok && l.p[1].n_models == 3);
    CHECK(!strcmp(l.p[1].models[1], "google/gemini-2.5-flash-lite"));
    CHECK(!live_llm_parse_json("{\"other\":1}", 11, &l));   /* no llm object */
    char req[512];
    size_t n = live_llm_request(req, sizeof req, "127.0.0.1", 8766, "groq", "llama-3.3-70b", false);
    CHECK(n > 0 && !strncmp(req, "POST /v1/settings HTTP/1.1\r\n", strlen("POST /v1/settings HTTP/1.1\r\n")));
    CHECK(strstr(req, "{\"llm\":{\"provider\":\"groq\",\"model\":\"llama-3.3-70b\",\"failover\":false}}") != NULL);
    n = live_llm_request(req, sizeof req, "127.0.0.1", 8766, "openrouter", NULL, true);   /* keep the model */
    CHECK(!strcmp(body_of(req, n), "{\"llm\":{\"provider\":\"openrouter\",\"failover\":true}}"));
}

int main(void)
{
    lv_init();
    cb_model_init();
    polyline();
    vehicle();
    turn();
    remote_fsm();
    mic_frames();
    session_requests();
    set_request();
    cabin_writes_shown();
    result_pins();
    position_label();
    timezone_follows_car();
    llm_settings();
    if(fails) return 1;
    printf("ok\n");
    return 0;
}
