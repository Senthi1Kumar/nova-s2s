#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "live/live.h"
#include "cb_app.h"
#include "cb_model.h"
#include "cb_theme.h"
#include "live/json.h"
#include "live/nav_state.h"
#include "live/ws.h"
#include "map/map_view.h"
#include "screens/cockpit.h"
#include "widgets/ui_kit.h"
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <netdb.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define ROUTE_MAX 6000
#define STOPS_MAX 8
#define CARDS_MAX 6
#define TOAST_MS 7000

typedef enum { LINK_OFF, LINK_CONNECTING, LINK_LIVE, LINK_REPLAY, LINK_OFFLINE } link_t;

typedef struct {
    cb_last_turn_t t;
    char utter[200], router[40], latency[40], api[3][56], args[3][96], path[3][96], change[3][96];
} turn_buf_t;

static struct {
    ws_client_t *ws;
    char host[128];
    int port;
    lv_timer_t *timer;
    link_t link;
    bool session_active;
    char **lines;   /* replay */
    uint32_t *t_ms;
    size_t n_lines, next;
    uint32_t t0;
    double speed;
    bool loop;
    uint32_t seq, gaps;   /* stream state */
    bool have_seq;
    int user_gen, reply_gen;
    char utter[200], reply[1024];
    double lat, lon, heading;
    bool have_lat, have_lon, pos_dirty;
    char pos_source[16];
    double nav_eta, nav_dist;
    bool nav_active;
    int gps_fix, gps_sats;   /* -1 unknown */
    int32_t fan[2];          /* driver, passenger */
    bool have_place;
    lv_timer_t *toast_timer;
} L = { .user_gen = -1, .reply_gen = -1, .gps_fix = -1, .gps_sats = -1 };

static turn_buf_t s_turn[2];
static int s_turn_i;
static double s_route[ROUTE_MAX][2];
static char s_congestion[ROUTE_MAX];

/* Turn-by-turn: the route's steps and progress, its stops for the map pins and the "via" line. */
static nav_state_t s_nav;
static double s_nav_total_m;
static char s_via[200];
static char s_stop_names[STOPS_MAX][96], s_dest_name[96];
static route_stop_t s_stops[STOPS_MAX], s_dest;
static char s_poi_names[STOPS_MAX][96];
static route_stop_t s_pois[STOPS_MAX];   /* the last place search, numbered in order */
static int s_n_pois;
/* Pins hide once one of them is chosen (the destination or a stop) or a trip starts after the search; a new
   search shows them again. */
static uint32_t s_poi_key, s_poi_hidden_key;
static int s_pins_shown;
static char s_trip[160];
static int s_n_stops;

/* ---------- helpers ---------- */

static cockpit_t *ck(void) { return cockpit(); }

static void set_link(link_t l)
{
    if(L.link == l) return;
    L.link = l;
    cockpit_t *c = ck();
    if(!c->link_chip) return;
    switch(l) {
        case LINK_CONNECTING: cockpit_set_chip(c->link_chip, "Connecting to Nova\xE2\x80\xA6", cb_col(CB_YELLOW)); break;
        case LINK_LIVE:
            cockpit_set_chip(c->link_chip, L.session_active ? "Live \xC2\xB7 Nova pipeline" : "Live \xC2\xB7 no voice client",
                             cb_col(L.session_active ? CB_GREEN : CB_YELLOW));
            break;
        case LINK_REPLAY: cockpit_set_chip(c->link_chip, "Replay \xC2\xB7 recorded session", cb_col(CB_VIOLET)); break;
        case LINK_OFFLINE: cockpit_set_chip(c->link_chip, "Nova offline \xC2\xB7 reconnecting", cb_col(CB_RED)); break;
        default: break;
    }
}

static void fmt_value(const cbj_t *v, char *out, size_t cap)
{
    if(!v) snprintf(out, cap, "-");
    else if(v->type == CBJ_NUM) snprintf(out, cap, "%g", v->n);
    else if(v->type == CBJ_STR) snprintf(out, cap, "%s", v->s);
    else if(v->type == CBJ_BOOL) snprintf(out, cap, "%s", v->b ? "on" : "off");
    else if(v->type == CBJ_NULL) snprintf(out, cap, "none");
    else snprintf(out, cap, "%s", v->type == CBJ_ARR ? "[..]" : "{..}");
}

static void bar_text(const char *text, bool user, bool stream)
{
    if(stream) cb_app_nova_stream(text);
    else cb_app_nova_text(text, user);
}

static void bar_state(const char *state)
{
    nova_fsm_t *f = cb_app_fsm();
    cb_app_nova_apply(nova_fsm_remote(f, state, lv_tick_get()));
}

static void bar_mute(const cbj_t *muted)
{
    if(!muted || muted->type != CBJ_BOOL) return;   /* malformed: keep the last known state */
    cb_app_nova_apply(nova_fsm_remote_mute(cb_app_fsm(), muted->b));
}

/* ---------- vehicle ---------- */

static bool num(const cbj_t *v, double *out)
{
    if(v && v->type == CBJ_NUM) *out = v->n;
    else if(v && v->type == CBJ_BOOL) *out = v->b;
    else return false;
    return true;
}

static int ambient_ix(const char *s)
{
    static const char *names[] = { "white", "blue", "red", "green", "purple", "orange", "pink" };
    for(int i = 0; i < 7; i++) if(s && !strcmp(s, names[i])) return i;
    return 0;
}

static int mode_ix(const char *s, const char *const *names, int n, int fallback)
{
    for(int i = 0; i < n; i++) if(s && !strcmp(s, names[i])) return i;
    return fallback;
}

static void set_defrost_bit(cb_model_t *m, int bit, bool on)
{
    int32_t cur = lv_subject_get_int(m->defrost);
    int32_t next = on ? (cur | bit) : (cur & ~bit);
    cb_model_set_int(m->defrost, next);
}

static int32_t clampi(double v, int32_t lo, int32_t hi)
{
    long r = lround(v);
    return (int32_t)(r < lo ? lo : r > hi ? hi : r);
}

/* The turn-by-turn card from the latest route, progress frame and 1 Hz ETA / distance deltas. */
static void nav_labels(void)
{
    if(!s_nav.active) return;
    s_nav.eta_min = L.nav_eta;
    s_nav.distance_km = L.nav_dist;
    nav_banner_t b;
    nav_banner(&s_nav, false, (long)time(NULL), &b);
    char line[sizeof b.turn_dist + sizeof b.instruction + 8];
    if(b.turn_dist[0] && b.instruction[0]) snprintf(line, sizeof line, "In %s \xC2\xB7 %s", b.turn_dist, b.instruction);
    else snprintf(line, sizeof line, "%s", b.instruction);
    cb_app_nova_route_line(line);
    double left = s_nav.has_progress ? s_nav.remaining_m : L.nav_dist * 1000.0;
    int permille = s_nav_total_m > 0 ? (int)lround(1000.0 * (1.0 - left / s_nav_total_m)) : 0;
    cockpit_set_nav(&b, s_via, permille);
}

/* One VSS path onto the cabin model; unknown paths and wrong types are ignored. */
static void apply(const char *p, const cbj_t *v, const char *source)
{
    cb_model_t *m = cb_model();
    double d;
    const char *s = cbj_str(v, NULL);
    static const char *const win[CB_WIN_COUNT] = { "Vehicle.Cabin.Door.Row1.DriverSide.Window.Position",
                                                   "Vehicle.Cabin.Door.Row1.PassengerSide.Window.Position",
                                                   "Vehicle.Cabin.Door.Row2.DriverSide.Window.Position",
                                                   "Vehicle.Cabin.Door.Row2.PassengerSide.Window.Position" };
    for(int i = 0; i < CB_WIN_COUNT; i++)
        if(!strcmp(p, win[i])) {
            if(num(v, &d)) cb_model_set_window((cb_window_t)i, clampi(d, 0, 100));
            return;
        }
    if(!strcmp(p, "Vehicle.Speed")) {
        if(num(v, &d)) cb_model_set_speed(clampi(d, 0, 250));
    }
    else if(!strcmp(p, "Vehicle.Powertrain.Transmission.SelectedGear")) {
        if(s && *s) {
            int g = s[0] == 'P' ? CB_GEAR_P : s[0] == 'R' ? CB_GEAR_R : s[0] == 'N' ? CB_GEAR_N : CB_GEAR_D;
            cb_model_set_int(m->gear, g);
        }
    }
    else if(!strcmp(p, "Vehicle.Powertrain.TractionBattery.StateOfCharge.Displayed")) {
        if(num(v, &d)) cb_model_set_int(m->soc_pct, clampi(d, 0, 100));
    }
    else if(!strcmp(p, "Vehicle.Powertrain.Range")) {
        if(num(v, &d)) cb_model_set_int(m->range_km, clampi(d > 1000 ? d / 1000 : d, 0, 2000));
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature")) {
        if(num(v, &d)) cb_model_set_temp(true, clampi(d * 10, 0, 400));
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.Station.Row1.Passenger.Temperature")) {
        if(num(v, &d)) cb_model_set_temp(false, clampi(d * 10, 0, 400));
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed")
            || !strcmp(p, "Vehicle.Cabin.HVAC.Station.Row1.Passenger.FanSpeed")) {
        /* One fan readout: the stronger zone, so a passenger-only change still shows. */
        if(num(v, &d)) {
            L.fan[strstr(p, "Passenger") != NULL] = clampi(d, 0, 6);
            cb_model_set_int(m->fan_level, L.fan[0] > L.fan[1] ? L.fan[0] : L.fan[1]);
        }
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.IsAirConditioningActive")) {
        if(num(v, &d)) cb_model_set_int(m->ac_on, d != 0);
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.IsFrontDefrosterActive")) {
        if(num(v, &d)) set_defrost_bit(m, 1, d != 0.0);
    }
    else if(!strcmp(p, "Vehicle.Cabin.HVAC.IsRearDefrosterActive")) {
        if(num(v, &d)) set_defrost_bit(m, 2, d != 0.0);
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Light.Ambient.Color")) {
        if(s) cb_model_set_int(m->ambient_color, ambient_ix(s));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Light.Ambient.Intensity")) {
        if(num(v, &d)) cb_model_set_int(m->ambient_level, clampi(d, 0, 100));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Lights.HeadlightMode")) {
        static const char *modes[] = { "off", "auto", "low", "high" };
        if(s) cb_model_set_int(m->headlights, mode_ix(s, modes, 4, 1));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Mirrors.IsFolded")) {
        if(num(v, &d)) cb_model_set_int(m->mirrors_folded, d != 0);
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.System.DriveMode")) {
        static const char *modes[] = { "eco", "comfort", "sport" };
        if(s) cb_model_set_int(m->drive_mode, mode_ix(s, modes, 3, 1));
    }
    else if(!strcmp(p, "Vehicle.Cabin.Sunroof.Position")) {
        if(num(v, &d)) cb_model_set_sunroof(clampi(d, -100, 100));   /* -100: tilted */
    }
    else if(!strcmp(p, "Vehicle.Cabin.Sunroof.Shade.Position")) {
        if(num(v, &d)) cb_model_set_int(m->sunshade_pct, clampi(d, 0, 100));
    }
    else if(!strcmp(p, "Vehicle.Cabin.Door.Row1.DriverSide.IsLocked")) {
        if(num(v, &d)) cb_model_set_int(m->locked, d != 0);
    }
    else if(!strcmp(p, "Vehicle.Cabin.Seat.Row1.DriverSide.Heating")) {
        if(num(v, &d)) cb_model_set_int(m->seat_heat, clampi(d, 0, 3));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Wipers.Mode")) {
        static const char *modes[] = { "off", "auto", "low", "high" };
        if(s) {
            int ix = mode_ix(s, modes, 4, 0);
            cb_model_set_int(m->wiper_mode, ix);
            cb_model_set_int(m->wipers_on, ix != 0);
        }
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Media.State")) {
        if(s) cb_model_set_int(m->media_playing, !strcmp(s, "playing"));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Media.TrackIndex")) {
        if(num(v, &d)) cb_model_set_int(m->media_track, (int32_t)d);
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Media.Title")) {
        if(s) cb_model_set_media_title(s);
    }
    else if(!strcmp(p, "Vehicle.Cabin.Infotainment.Media.Volume")) {
        if(num(v, &d)) cb_model_set_int(m->volume, clampi(d, 0, 100));
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Phone.CallState")) {
        if(s) cb_model_set_int(m->call_active, !strcmp(s, "active") || !strcmp(s, "dialing"));
    }
    else if(!strcmp(p, "Vehicle.CurrentLocation.Latitude")) {
        if(num(v, &d)) L.lat = d, L.have_lat = L.pos_dirty = true;
    }
    else if(!strcmp(p, "Vehicle.CurrentLocation.Longitude")) {
        if(num(v, &d)) L.lon = d, L.have_lon = L.pos_dirty = true;
    }
    else if(!strcmp(p, "Vehicle.CurrentLocation.Heading")) {
        if(num(v, &d)) L.heading = d, L.pos_dirty = true;
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Nav.EtaMin")) {
        if(num(v, &d)) L.nav_eta = d, nav_labels();
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Nav.DistanceKm")) {
        if(num(v, &d)) L.nav_dist = d, nav_labels();
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Location.Place")) {
        if(s && *s && ck()->place) {
            lv_label_set_text(ck()->place, s);
            L.have_place = true;
        }
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Location.TimeZone")) {
        if(s) live_set_timezone(s);
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Location.GpsFix")) {
        if(num(v, &d)) L.gps_fix = d != 0, L.pos_dirty = true;
    }
    else if(!strcmp(p, "Vehicle.Private.Nova.Location.Satellites")) {
        if(num(v, &d)) L.gps_sats = (int)d, L.pos_dirty = true;
    }
    if(source && !strncmp(p, "Vehicle.CurrentLocation.", 24)) snprintf(L.pos_source, sizeof L.pos_source, "%s", source);
}

static void flush_position(void)
{
    if(!L.pos_dirty) return;
    L.pos_dirty = false;
    cockpit_t *c = ck();
    if(c->map && L.have_lat && L.have_lon) map_view_set_car(c->map, L.lat, L.lon, L.heading);
    bool gps = false;
    if(c->src_chip) {
        char t[80];
        gps = live_position_label(L.gps_fix, L.gps_sats, L.pos_source, L.nav_active, t, sizeof t);
        cockpit_set_chip(c->src_chip, t, cb_col(gps ? CB_GREEN : CB_YELLOW));
    }
    if(c->place && !L.have_place && gps) {
        char t[48];
        snprintf(t, sizeof t, "%.4f, %.4f", L.lat, L.lon);
        lv_label_set_text(c->place, t);
    }
}

bool live_set_timezone(const char *zone)
{
    if(!zone || !*zone || strlen(zone) > 64 || zone[0] == '/' || strstr(zone, "..")) return false;
    for(const char *c = zone; *c; c++)
        if(!isalnum((unsigned char)*c) && !strchr("/_+-", *c)) return false;
    const char *now = getenv("TZ");
    if(now && !strcmp(now, zone)) return true;
    setenv("TZ", zone, 1);
    tzset();
    return true;
}

bool live_position_label(int fix, int sats, const char *source, bool driving, char *out, size_t cap)
{
    bool gps = fix >= 0 ? fix == 1 : (source && !strcmp(source, "gps"));
    if(gps && driving) {
        snprintf(out, cap, "Simulated drive \xC2\xB7 GPS %d sats", sats);
        return false;
    }
    if(gps && sats > 0) snprintf(out, cap, "GPS \xC2\xB7 %d sats", sats);
    else if(gps) snprintf(out, cap, "GPS \xC2\xB7 fix");
    else if(fix == 0) snprintf(out, cap, "GPS \xC2\xB7 no fix \xC2\xB7 simulated drive");
    else snprintf(out, cap, "Position \xC2\xB7 %s", source && source[0] ? "simulated drive" : "live");
    return gps;
}

static void on_vehicle(const cbj_t *f)
{
    const cbj_t *snap = cbj_get(f, "snapshot"), *ch = cbj_get(f, "changed");
    uint32_t seq = (uint32_t)cbj_num(cbj_get(f, "seq"), 0);
    if(snap) {
        for(const cbj_t *c = snap->child; c; c = c->next)
            apply(c->key, cbj_get(c, "value"), cbj_str(cbj_get(c, "source"), NULL));
        L.seq = seq;
        L.have_seq = true;
    }
    else if(ch) {
        if(L.have_seq && seq != L.seq && seq != L.seq + 1) {   /* the server dropped events for us: start over */
            L.gaps++;
            L.seq = seq;
            if(L.ws) ws_client_reconnect(L.ws);
            return;
        }
        for(const cbj_t *c = ch->child; c; c = c->next) apply(c->key, c, NULL);
        L.seq = seq;
        L.have_seq = true;
    }
    flush_position();
}

/* ---------- turns ---------- */

static cb_verdict_t verdict(const cbj_t *call)
{
    const char *v = cbj_str(cbj_get(cbj_get(call, "gate"), "verdict"), NULL);
    if(!v) v = cbj_str(cbj_get(call, "status"), "");
    if(!strcmp(v, "execute") || !strcmp(v, "success") || !strcmp(v, "accepted")) return CB_VERDICT_EXECUTE;
    if(!strcmp(v, "confirm_first") || !strcmp(v, "clarify") || !strcmp(v, "pending")) return CB_VERDICT_CONFIRM;
    return CB_VERDICT_DECLINE;
}

static const char *short_path(const char *p)
{
    const char *seg[3] = { p, p, p };   /* the last three dot-separated segments */
    for(const char *q = p; *q; q++)
        if(*q == '.') {
            seg[0] = seg[1];
            seg[1] = seg[2];
            seg[2] = q + 1;
        }
    return seg[0];
}

static void on_trace(const cbj_t *f)
{
    turn_buf_t *b = &s_turn[s_turn_i];
    s_turn_i ^= 1;
    memset(b, 0, sizeof *b);
    int gen = (int)cbj_num(cbj_get(f, "generation_id"), -2);
    snprintf(b->utter, sizeof b->utter, "%s", gen == L.user_gen ? L.utter : cbj_str(cbj_get(f, "utterance"), ""));
    snprintf(b->router, sizeof b->router, "%s", cbj_str(cbj_get(f, "router"), "?"));   /* the card adds "router ·" */
    const cbj_t *lat = cbj_get(f, "latency_ms");
    size_t ll = 0;
    for(const cbj_t *c = lat ? lat->child : NULL; c && ll < sizeof b->latency; c = c->next)
        if(c->type == CBJ_NUM) ll += (size_t)snprintf(b->latency + ll, sizeof b->latency - ll, "%s%.0f", ll ? " + " : "", c->n);
    if(ll && ll < sizeof b->latency) snprintf(b->latency + ll, sizeof b->latency - ll, " ms");
    const cbj_t *calls = cbj_get(f, "calls");
    int n = 0;
    for(const cbj_t *c = calls ? calls->child : NULL; c && n < 3; c = c->next, n++) {
        snprintf(b->api[n], sizeof b->api[n], "%s \xC2\xB7 %s", cbj_str(cbj_get(c, "device"), "?"),
                 cbj_str(cbj_get(c, "api"), "?"));
        size_t al = 0;
        const cbj_t *args = cbj_get(c, "args");
        for(const cbj_t *a = args ? args->child : NULL; a && al < sizeof b->args[n]; a = a->next) {
            char v[48];
            fmt_value(a, v, sizeof v);
            al += (size_t)snprintf(b->args[n] + al, sizeof b->args[n] - al, "%s%s=%s", al ? " " : "", a->key, v);
        }
        b->t.calls[n] = (cb_call_t){ b->api[n], b->args[n], verdict(c) };
    }
    const cbj_t *diff = cbj_get(f, "diff");
    int d = 0;
    for(const cbj_t *c = diff ? diff->child : NULL; c && d < 3; c = c->next, d++) {
        snprintf(b->path[d], sizeof b->path[d], "%s", short_path(cbj_str(cbj_get(c, "path"), "?")));
        char x[40], y[40];
        fmt_value(cbj_get(c, "before"), x, sizeof x);
        fmt_value(cbj_get(c, "after"), y, sizeof y);
        snprintf(b->change[d], sizeof b->change[d], "%s \xE2\x86\x92 %s", x, y);
        b->t.diffs[d] = (cb_diff_t){ b->path[d], b->change[d] };
    }
    b->t.utterance = b->utter;
    b->t.router = b->router;
    b->t.latency = b->latency;
    b->t.n_calls = n;
    b->t.n_diffs = d;
    cb_model_set_last_turn(&b->t);
}

/* ---------- cards ---------- */

static void toast_hide(lv_timer_t *t)
{
    LV_UNUSED(t);
    L.toast_timer = NULL;
    if(ck()->toast) lv_obj_set_hidden(ck()->toast, true);
}

static void toast_arm(uint32_t ms)
{
    if(L.toast_timer) lv_timer_delete(L.toast_timer);
    L.toast_timer = lv_timer_create(toast_hide, ms, NULL);
    lv_timer_set_repeat_count(L.toast_timer, 1);
}

void live_toast(const char *title, const char *detail)
{
    cockpit_t *c = ck();
    if(!c->toast) return;   /* frame D has no toast */
    lv_obj_clean(c->toast);
    ui_label(c->toast, title, CB_F_VALUE, CB_TEXT_HI);
    if(detail && *detail) ui_label(c->toast, detail, CB_F_BODY, CB_TEXT_2);
    lv_obj_set_hidden(c->toast, false);
    toast_arm(4000);
}

static void fill_card(lv_obj_t *box, const cbj_t *f, int max_lines)
{
    lv_obj_t *t = ui_label(box, cbj_str(cbj_get(f, "title"), "Nova"), CB_F_VALUE, CB_TEXT_HI);
    lv_obj_set_width(t, lv_pct(100));
    lv_label_set_long_mode(t, LV_LABEL_LONG_MODE_WRAP);
    const cbj_t *lines = cbj_get(f, "lines");
    int k = 0;
    for(const cbj_t *l = lines ? lines->child : NULL; l && k < max_lines; l = l->next, k++) {
        lv_obj_t *x = ui_label(box, cbj_str(l, ""), CB_F_BODY, CB_TEXT_2);
        lv_obj_set_width(x, lv_pct(100));
        lv_label_set_long_mode(x, LV_LABEL_LONG_MODE_WRAP);
    }
    const cbj_t *items = cbj_get(f, "items");
    k = 0;
    for(const cbj_t *it = items ? items->child : NULL; it && k < 5; it = it->next, k++) {
        lv_obj_t *row = ui_row(box, 10);
        ui_dot(row, CB_CYAN, false);
        ui_label(row, cbj_str(cbj_get(it, "title"), ""), CB_F_BODY, CB_TEXT_HI);
        ui_label(row, cbj_str(cbj_get(it, "sub"), ""), CB_F_LABEL, CB_TEXT_MID);
    }
}

/* The v2 panel's table: title, up to two lines, the first rows and how many more. */
static void card_view(const cbj_t *f)
{
    nova_card_view_t v = { .kind = cbj_str(cbj_get(f, "kind"), "answer"), .title = cbj_str(cbj_get(f, "title"), "Nova") };
    const cbj_t *lines = cbj_get(f, "lines");
    for(const cbj_t *l = lines ? lines->child : NULL; l && v.n_lines < 2; l = l->next) v.lines[v.n_lines++] = cbj_str(l, "");
    const cbj_t *items = cbj_get(f, "items");
    for(const cbj_t *it = items ? items->child : NULL; it; it = it->next) {
        if(v.n_rows == NOVA_CARD_ROWS) {
            v.more++;
            continue;
        }
        v.rows[v.n_rows].title = cbj_str(cbj_get(it, "title"), "");
        v.rows[v.n_rows].sub = cbj_str(cbj_get(it, "sub"), "");
        v.n_rows++;
    }
    cb_app_nova_card(&v);
}

static void on_card(const cbj_t *f)
{
    card_view(f);
    cockpit_t *c = ck();
    if(c->cards) {
        lv_obj_t *card = ui_card(c->cards);
        lv_obj_move_to_index(card, 0);
        lv_obj_t *k = ui_label(card, cbj_str(cbj_get(f, "kind"), "answer"), CB_F_SMALL, CB_TEXT_LO);
        lv_obj_set_style_text_letter_space(k, 2, 0);
        fill_card(card, f, 4);
        while(lv_obj_get_child_count(c->cards) > CARDS_MAX) lv_obj_delete(lv_obj_get_child(c->cards, -1));
    }
    if(c->toast && !cb_app_panel()) {   /* v2 shows it in the Nova panel */
        lv_obj_clean(c->toast);
        fill_card(c->toast, f, 2);
        lv_obj_set_hidden(c->toast, false);
        toast_arm(TOAST_MS);
    }
}

/* ---------- navigation ---------- */

int live_polyline6(const char *s, double (*ll)[2], int cap)
{
    int n = 0;
    long lat = 0, lon = 0;
    const char *p = s;
    while(*p && n < cap) {
        long d[2];
        for(int k = 0; k < 2; k++) {
            long v = 0;
            int shift = 0, b;
            do {
                if(!*p) return n;
                b = *p++ - 63;
                v |= (long)(b & 0x1f) << shift;
                shift += 5;
            } while(b >= 0x20);
            d[k] = (v & 1) ? ~(v >> 1) : (v >> 1);
        }
        lat += d[0];
        lon += d[1];
        ll[n][0] = lon / 1e6;
        ll[n][1] = lat / 1e6;
        n++;
    }
    return n;
}

/* Remaining stops in order, for the map pins and the banner's "via" line. */
static void nav_stops(const cbj_t *f)
{
    s_n_stops = 0;
    s_via[0] = '\0';
    const cbj_t *stops = cbj_get(f, "stops");
    for(const cbj_t *st = stops ? stops->child : NULL; st && s_n_stops < STOPS_MAX; st = st->next) {
        const cbj_t *lat = cbj_get(st, "lat"), *lon = cbj_get(st, "lon");
        if(!lat || !lon) continue;
        int i = s_n_stops++;
        snprintf(s_stop_names[i], sizeof s_stop_names[i], "%s", cbj_str(cbj_get(st, "name"), "Stop"));
        s_stops[i] = (route_stop_t){ s_stop_names[i], cbj_num(lat, 0), cbj_num(lon, 0) };
        size_t len = strlen(s_via);
        snprintf(s_via + len, sizeof s_via - len, "%s%s", i ? " \xC2\xB7 " : "", s_stop_names[i]);
    }
}

static bool near_m(const route_stop_t *a, double lat, double lon, double m)
{
    double dy = (a->lat - lat) * 111320.0, dx = (a->lon - lon) * 111320.0 * cos(lat * M_PI / 180.0);
    return dx * dx + dy * dy <= m * m;
}

/* Called once stops and destination are known: decides whether the pins still belong on the map. */
static void show_pois(lv_obj_t *map, bool active, const route_stop_t *dest)
{
    char trip[160] = "";
    if(active && dest) snprintf(trip, sizeof trip, "%s@%.4f,%.4f", dest->name, dest->lat, dest->lon);
    if(strcmp(trip, s_trip) != 0) {   /* a trip started, changed or ended: earlier results are done */
        snprintf(s_trip, sizeof s_trip, "%s", trip);
        s_poi_hidden_key = s_poi_key;
    }
    for(int i = 0; active && i < s_n_pois; i++) {   /* one was chosen: it is the destination or a stop now */
        bool chosen = dest && near_m(&s_pois[i], dest->lat, dest->lon, 60);
        for(int k = 0; !chosen && k < s_n_stops; k++) chosen = near_m(&s_pois[i], s_stops[k].lat, s_stops[k].lon, 60);
        if(chosen) s_poi_hidden_key = s_poi_key;
    }
    s_pins_shown = s_poi_key && s_poi_key != s_poi_hidden_key ? s_n_pois : 0;
    if(map) map_view_set_results(map, s_pois, s_pins_shown);
}

int live_pins_shown(void) { return s_pins_shown; }

/* The last place search, kept for show_pois. */
static void nav_pois(const cbj_t *f)
{
    int n = 0;
    const cbj_t *pois = cbj_get(f, "pois");
    for(const cbj_t *p = pois ? pois->child : NULL; p && n < STOPS_MAX; p = p->next) {
        const cbj_t *lat = cbj_get(p, "lat"), *lon = cbj_get(p, "lon");
        if(!lat || !lon) continue;
        snprintf(s_poi_names[n], sizeof s_poi_names[n], "%s", cbj_str(cbj_get(p, "name"), "Place"));
        s_pois[n] = (route_stop_t){ s_poi_names[n], cbj_num(lat, 0), cbj_num(lon, 0) };
        n++;
    }
    uint32_t key = n ? 2166136261u ^ (uint32_t)n : 0;   /* changes with the set: a new search */
    for(int i = 0; i < n; i++) {
        key = (key ^ (uint32_t)llround(s_pois[i].lat * 1e5)) * 16777619u;
        key = (key ^ (uint32_t)llround(s_pois[i].lon * 1e5)) * 16777619u;
    }
    s_poi_key = n ? (key ? key : 1) : 0;
    s_n_pois = n;
}

static void on_nav(const cbj_t *f)
{
    cockpit_t *c = ck();
    bool was = L.nav_active;
    L.nav_active = cbj_bool(cbj_get(f, "active"), false);
    L.pos_dirty = true;
    flush_position();
    nav_pois(f);
    nav_state_from_frame(&s_nav, f);
    if(!L.nav_active) {
        cockpit_set_nav(NULL, NULL, 0);
        cb_app_nova_route_line(NULL);
        show_pois(c->map, false, NULL);
        if(c->map) {
            map_view_set_route(c->map, NULL, 0, NULL);
            map_view_set_stops(c->map, NULL, 0, NULL);
            map_view_set_nav(c->map, false);
        }
        return;
    }
    L.nav_eta = s_nav.eta_min;
    L.nav_dist = s_nav.distance_km;
    s_nav_total_m = s_nav.distance_m > 0 ? s_nav.distance_m : s_nav.distance_km * 1000.0;
    nav_stops(f);
    const cbj_t *dest = cbj_get(f, "destination");
    snprintf(s_dest_name, sizeof s_dest_name, "%s", s_nav.dest);
    bool have_dest = cbj_get(dest, "lat") && cbj_get(dest, "lon");
    s_dest = (route_stop_t){ s_dest_name, cbj_num(cbj_get(dest, "lat"), 0), cbj_num(cbj_get(dest, "lon"), 0) };
    snprintf(s_congestion, sizeof s_congestion, "%s", cbj_str(cbj_get(f, "congestion"), ""));
    nav_labels();
    int n = live_polyline6(cbj_str(cbj_get(f, "route"), ""), s_route, ROUTE_MAX);
    if(c->map) {
        if(!was) map_view_set_follow(c->map, true);   /* a new trip starts on the car */
        map_view_set_nav(c->map, true);
        map_view_set_route(c->map, (const double (*)[2])s_route, n, s_congestion[0] ? s_congestion : NULL);
        map_view_set_stops(c->map, s_stops, s_n_stops, have_dest ? &s_dest : NULL);
    }
    show_pois(c->map, true, have_dest ? &s_dest : NULL);
}

static bool llm_from(const cbj_t *llm, live_llm_t *out);
static bool tts_from(const cbj_t *f, live_tts_t *out);
static void (*s_llm_cb)(const live_llm_t *);
static void (*s_tts_cb)(const live_tts_t *);
static void (*s_guidance_cb)(const char *);

void live_set_guidance_cb(void (*cb)(const char *text)) { s_guidance_cb = cb; }

/* The mood event a frame stands for, or -1. A session start is decided by dispatch (only the first counts). */
static int mood_event_from(const char *type, const cbj_t *f)
{
    if(!strcmp(type, "observe.session")) return cbj_bool(cbj_get(f, "active"), false) ? NM_SESSION_START : -1;
    if(!strcmp(type, "input_audio_buffer.speech_started")) return NM_SPEECH_STARTED;
    if(!strcmp(type, "conversation.item.input_audio_transcription.completed")) return NM_TRANSCRIPT_DONE;
    if(!strcmp(type, "nova.tool_status")) {
        const char *st = cbj_str(cbj_get(f, "status"), "");
        return !strcmp(st, "error") || !strcmp(st, "failed") ? NM_TOOL_ERROR : -1;
    }
    if(!strcmp(type, "nova.agent_trace")) return *cbj_str(cbj_get(f, "error"), "") ? NM_TOOL_ERROR : -1;
    if(!strcmp(type, "response.audio_transcript.delta")) return cbj_bool(cbj_get(f, "nav_guidance"), false) ? -1 : NM_REPLY_DELTA;
    if(!strcmp(type, "output_audio.duck")) return cbj_num(cbj_get(f, "gain"), 1.0) < 1.0 ? NM_DUCK : NM_UNDUCK;
    if(!strcmp(type, "playback.interrupted")) return NM_INTERRUPTED;
    if(!strcmp(type, "response.done"))
        return cbj_bool(cbj_get(cbj_get(cbj_get(f, "response"), "metadata"), "nav_guidance"), false) ? -1 : NM_RESPONSE_DONE;
    return -1;
}

/* ---------- dispatch ---------- */

static void dispatch(const cbj_t *f)
{
    const char *type = cbj_str(cbj_get(f, "type"), "");
    int mood = strcmp(type, "observe.session") ? mood_event_from(type, f) : -1;
    if(mood >= 0) cb_app_nova_mood_event((nm_event_t)mood);
    if(!strcmp(type, "output_audio.duck")) cb_app_nova_duck((float)cbj_num(cbj_get(f, "gain"), 1.0));
    if(!strcmp(type, "nova.vehicle")) on_vehicle(f);
    else if(!strcmp(type, "nova.fsm")) bar_state(cbj_str(cbj_get(cbj_get(f, "fsm"), "state"), "IDLE"));
    else if(!strcmp(type, "nova.mic")) bar_mute(cbj_get(f, "muted"));
    else if(!strcmp(type, "conversation.item.input_audio_transcription.completed")) {
        L.user_gen = (int)cbj_num(cbj_get(f, "generation_id"), -1);
        snprintf(L.utter, sizeof L.utter, "%s", cbj_str(cbj_get(f, "transcript"), ""));
        bar_text(L.utter, true, false);
    }
    else if(cbj_bool(cbj_get(f, "nav_guidance"), false) && !strncmp(type, "response.audio_transcript.", 26)) {
        if(!strcmp(type, "response.audio_transcript.done")) {   /* a route prompt: the dock's line, not a reply */
            const char *line = cbj_str(cbj_get(f, "transcript"), "");
            cb_app_nova_guidance(line);
            if(s_guidance_cb) s_guidance_cb(line);
        }
    }
    else if(!strcmp(type, "response.audio_transcript.delta")) {
        int gen = (int)cbj_num(cbj_get(f, "generation_id"), -1);
        if(gen != L.reply_gen) {
            L.reply_gen = gen;
            L.reply[0] = '\0';
        }
        size_t len = strlen(L.reply);
        snprintf(L.reply + len, sizeof L.reply - len, "%s", cbj_str(cbj_get(f, "delta"), ""));
        bar_text(L.reply, false, true);
    }
    else if(!strcmp(type, "response.audio_transcript.done")) {
        L.reply_gen = (int)cbj_num(cbj_get(f, "generation_id"), -1);
        snprintf(L.reply, sizeof L.reply, "%s", cbj_str(cbj_get(f, "transcript"), ""));
        bar_text(L.reply, false, true);
    }
    else if(!strcmp(type, "nova.agent_trace")) on_trace(f);
    else if(!strcmp(type, "nova.card")) on_card(f);
    else if(!strcmp(type, "nova.nav")) on_nav(f);
    else if(!strcmp(type, "nova.nav.progress")) {
        nav_state_progress(&s_nav, f);
        nav_labels();
    }
    else if(!strcmp(type, "nova.settings")) {
        live_llm_t l;
        if(s_llm_cb && llm_from(cbj_get(f, "llm"), &l)) s_llm_cb(&l);
        live_tts_t t;
        if(tts_from(f, &t)) {
            if(s_tts_cb) s_tts_cb(&t);
            cb_app_set_avatar(t.avatar[0] ? t.avatar : NULL, t.voice[0] ? t.voice : NULL);
        }
    }
    else if(!strcmp(type, "observe.hello") || !strcmp(type, "observe.session")) {
        const cbj_t *a = cbj_get(f, !strcmp(type, "observe.hello") ? "session_active" : "active");
        bool was = L.session_active;
        L.session_active = cbj_bool(a, L.session_active);
        if(L.session_active && !was) {   /* a voice client joined: Nova says hi */
            cb_app_nova_mood_event(NM_SESSION_START);
            cb_app_nova_greet();
        }
        link_t l = L.link;
        L.link = LINK_OFF;   /* force a chip refresh */
        set_link(l == LINK_REPLAY ? LINK_REPLAY : LINK_LIVE);
    }
}

void live_feed(const char *json, size_t len)
{
    cbj_t *r = cbj_parse(json, len);
    if(!r) return;
    const cbj_t *frame = cbj_get(r, "frame");   /* replay lines wrap the frame */
    dispatch(frame && cbj_get(r, "t_ms") ? frame : r);
    cbj_free(r);
}

int live_mood_event(const char *json, size_t len)
{
    cbj_t *r = cbj_parse(json, len);
    if(!r) return -1;
    const cbj_t *frame = cbj_get(r, "frame");
    const cbj_t *f = frame && cbj_get(r, "t_ms") ? frame : r;
    int ev = mood_event_from(cbj_str(cbj_get(f, "type"), ""), f);
    cbj_free(r);
    return ev;
}

uint32_t live_seq(void) { return L.seq; }
uint32_t live_seq_gaps(void) { return L.gaps; }
bool live_active(void) { return L.ws || L.lines; }

/* ---------- sources ---------- */

static void on_text(void *ud, const char *text, size_t len)
{
    LV_UNUSED(ud);
    if(L.link != LINK_LIVE) set_link(LINK_LIVE);
    live_feed(text, len);
}

static void tick(lv_timer_t *t)
{
    LV_UNUSED(t);
    uint32_t now = lv_tick_get();
    if(L.ws) {
        ws_state_t st = ws_client_poll(L.ws, now);
        if(st == WS_CLOSED && (L.link == LINK_LIVE || L.link == LINK_CONNECTING)) {
            if(L.link == LINK_LIVE) bar_state("IDLE");
            L.have_seq = false;
            set_link(LINK_OFFLINE);
        }
        return;
    }
    if(L.lines) {
        uint32_t elapsed = (uint32_t)((now - L.t0) * L.speed);
        while(L.next < L.n_lines && L.t_ms[L.next] <= elapsed) {
            live_feed(L.lines[L.next], strlen(L.lines[L.next]));
            L.next++;
        }
        if(L.next >= L.n_lines && L.loop && elapsed > L.t_ms[L.n_lines - 1] + 3000) {
            L.next = 0;
            L.t0 = now;
            L.have_seq = false;
        }
    }
}

bool live_start(const char *url)
{
    char path[256];
    if(!ws_parse_url(url, L.host, sizeof L.host, &L.port, path, sizeof path)) {
        fprintf(stderr, "live: %s is not a ws:// url (wss needs TLS: run on the Pi or tunnel with "
                        "ssh -L 8766:127.0.0.1:8766 <pi>)\n", url);
        return false;
    }
    L.ws = ws_client_create(url, on_text, NULL);
    if(!L.ws) return false;
    set_link(LINK_CONNECTING);
    L.timer = lv_timer_create(tick, 20, NULL);
    return true;
}

bool live_start_replay(const char *path, double speed, bool loop)
{
    FILE *fp = fopen(path, "r");
    if(!fp) {
        fprintf(stderr, "live: cannot open %s\n", path);
        return false;
    }
    size_t cap = 0, lcap = 0;
    char *line = NULL;
    ssize_t n;
    while((n = getline(&line, &lcap, fp)) > 0) {
        while(n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = '\0';
        if(!n) continue;
        cbj_t *r = cbj_parse(line, (size_t)n);
        if(!r) continue;
        uint32_t t = (uint32_t)cbj_num(cbj_get(r, "t_ms"), 0);
        cbj_free(r);
        if(L.n_lines == cap) {
            cap = cap ? cap * 2 : 256;
            L.lines = realloc(L.lines, cap * sizeof *L.lines);
            L.t_ms = realloc(L.t_ms, cap * sizeof *L.t_ms);
        }
        L.lines[L.n_lines] = strdup(line);
        L.t_ms[L.n_lines++] = t;
    }
    free(line);
    fclose(fp);
    if(!L.n_lines) return false;
    L.speed = speed > 0 ? speed : 1;
    L.loop = loop;
    L.t0 = lv_tick_get();
    set_link(LINK_REPLAY);
    L.timer = lv_timer_create(tick, 20, NULL);
    return true;
}

/* ---------- settings ---------- */

void live_on_llm(void (*cb)(const live_llm_t *llm)) { s_llm_cb = cb; }

bool live_linked(void) { return L.ws && L.link == LINK_LIVE; }

static bool llm_from(const cbj_t *llm, live_llm_t *out)
{
    if(!llm || llm->type != CBJ_OBJ) return false;
    memset(out, 0, sizeof *out);
    snprintf(out->provider, sizeof out->provider, "%s", cbj_str(cbj_get(llm, "provider"), ""));
    snprintf(out->model, sizeof out->model, "%s", cbj_str(cbj_get(llm, "model"), ""));
    out->failover = cbj_bool(cbj_get(llm, "failover"), false);
    out->failover_ready = cbj_bool(cbj_get(llm, "failover_ready"), false);
    const cbj_t *ps = cbj_get(llm, "providers");
    for(const cbj_t *p = ps ? ps->child : NULL; p && out->n < LIVE_LLM_PROVIDERS; p = p->next) {
        int i = out->n++;
        snprintf(out->p[i].id, sizeof out->p[i].id, "%s", cbj_str(cbj_get(p, "id"), "?"));
        out->p[i].ok = cbj_bool(cbj_get(p, "ok"), true);
        const cbj_t *ms = cbj_get(p, "models");
        for(const cbj_t *m = ms ? ms->child : NULL; m && out->p[i].n_models < LIVE_LLM_MODELS; m = m->next)
            if(m->type == CBJ_STR)
                snprintf(out->p[i].models[out->p[i].n_models++], sizeof out->p[i].models[0], "%s", m->s);
    }
    return true;
}

bool live_llm_parse_json(const char *json, size_t len, live_llm_t *out)
{
    cbj_t *r = cbj_parse(json, len);
    bool ok = r && llm_from(cbj_get(r, "llm"), out);
    cbj_free(r);
    return ok;
}

size_t live_llm_request(char *out, size_t cap, const char *host, int port, const char *provider, const char *model,
                        bool failover)
{
    char body[320], model_kv[128] = "";
    if(model) snprintf(model_kv, sizeof model_kv, "\"model\":\"%s\",", model);   /* none: the server keeps its model */
    int bl = snprintf(body, sizeof body, "{\"llm\":{\"provider\":\"%s\",%s\"failover\":%s}}", provider, model_kv,
                      failover ? "true" : "false");
    int n = snprintf(out, cap,
                     "POST /v1/settings HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n\r\n%s",
                     host, port, bl, body);
    return n < 0 || (size_t)n >= cap ? 0 : (size_t)n;
}

/* One loopback HTTP exchange with bounded waits; returns the parsed JSON body or NULL with a reason. Settings and
   mic requests run on their own worker threads, so the response buffer is per call. */
static cbj_t *http_json(const char *req, size_t n, int wait_ms, char *err, size_t err_cap)
{
    char resp[8192];
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM }, *ai = NULL;
    char port[8];
    snprintf(port, sizeof port, "%d", L.port);
    if(!L.ws || !n || getaddrinfo(L.host, port, &hints, &ai) != 0 || !ai) {
        snprintf(err, err_cap, "no live link to Nova");
        return NULL;
    }
    int fd = socket(ai->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int r = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    struct pollfd p = { fd, POLLOUT, 0 };
    if((r != 0 && errno != EINPROGRESS) || poll(&p, 1, 300) <= 0 || send(fd, req, n, MSG_NOSIGNAL) != (ssize_t)n) {
        close(fd);
        snprintf(err, err_cap, "Nova is not reachable");
        return NULL;
    }
    size_t got = 0;
    p.events = POLLIN;
    while(got < sizeof resp - 1 && poll(&p, 1, wait_ms) > 0) {
        ssize_t k = recv(fd, resp + got, sizeof resp - 1 - got, 0);
        if(k <= 0) break;
        got += (size_t)k;
    }
    close(fd);
    resp[got] = '\0';
    const char *body = strstr(resp, "\r\n\r\n");
    cbj_t *j = body ? cbj_parse(body + 4, strlen(body + 4)) : NULL;
    if(!j)
        snprintf(err, err_cap, "%s",
                 strncmp(resp, "HTTP/1.1 404", 12) ? "no answer from Nova" : "this Nova pipeline is too old for that");
    return j;
}

bool live_llm_fetch(live_llm_t *out, char *err, size_t err_cap)
{
    char req[256];
    int n = snprintf(req, sizeof req, "GET /v1/settings HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n",
                     L.host, L.port);
    cbj_t *j = http_json(req, (size_t)n, 900, err, err_cap);
    bool ok = j && llm_from(cbj_get(j, "llm"), out);
    if(j && !ok) snprintf(err, err_cap, "unexpected settings reply");
    cbj_free(j);
    return ok;
}

bool live_llm_apply(const char *provider, const char *model, bool failover, live_llm_t *now, char *err, size_t err_cap)
{
    char req[640];
    size_t n = live_llm_request(req, sizeof req, L.host, L.port, provider, model, failover);
    cbj_t *j = http_json(req, n, 20000, err, err_cap);   /* switching to the on-device model loads it first */
    bool ok = j && cbj_bool(cbj_get(j, "ok"), false);
    if(now) {
        memset(now, 0, sizeof *now);
        if(j) llm_from(cbj_get(cbj_get(j, "settings"), "llm"), now);   /* sent on failure too */
    }
    if(j && !ok) snprintf(err, err_cap, "%s", cbj_str(cbj_get(j, "error"), "rejected"));
    cbj_free(j);
    return ok;
}

/* ---------- voice and avatar ---------- */

void live_on_tts(void (*cb)(const live_tts_t *tts)) { s_tts_cb = cb; }

/* Reads {"tts":{...},"avatar":".."}; false when the frame has neither (an older pipeline). */
static bool tts_from(const cbj_t *f, live_tts_t *out)
{
    const cbj_t *t = cbj_get(f, "tts"), *a = cbj_get(f, "avatar");
    bool has_t = t && t->type == CBJ_OBJ, has_a = a && a->type == CBJ_STR;
    if(!has_t && !has_a) return false;
    memset(out, 0, sizeof *out);
    if(has_a) snprintf(out->avatar, sizeof out->avatar, "%s", a->s);
    if(!has_t) return true;
    snprintf(out->voice, sizeof out->voice, "%s", cbj_str(cbj_get(t, "voice"), ""));
    snprintf(out->speed, sizeof out->speed, "%s", cbj_str(cbj_get(t, "speed"), ""));
    const cbj_t *vs = cbj_get(t, "voices");
    for(const cbj_t *v = vs ? vs->child : NULL; v && out->n < LIVE_TTS_VOICES; v = v->next) {
        int i = out->n++;
        snprintf(out->v[i].id, sizeof out->v[i].id, "%s", cbj_str(cbj_get(v, "id"), ""));
        snprintf(out->v[i].label, sizeof out->v[i].label, "%s", cbj_str(cbj_get(v, "label"), out->v[i].id));
    }
    return true;
}

bool live_tts_parse_json(const char *json, size_t len, live_tts_t *out)
{
    cbj_t *r = cbj_parse(json, len);
    bool ok = r && tts_from(r, out);
    cbj_free(r);
    return ok;
}

/* Appends s as a JSON string body (quotes, backslashes and control bytes escaped). */
static size_t json_esc(char *out, size_t cap, const char *s)
{
    size_t n = 0;
    for(; *s && n + 7 < cap; s++) {
        unsigned char c = (unsigned char)*s;
        if(c == '"' || c == '\\') n += (size_t)snprintf(out + n, cap - n, "\\%c", c);
        else if(c < 0x20) n += (size_t)snprintf(out + n, cap - n, "\\u%04x", c);
        else out[n++] = (char)c;
    }
    out[n] = '\0';
    return n;
}

static size_t post_json(char *out, size_t cap, const char *path, const char *host, int port, const char *body)
{
    int n = snprintf(out, cap,
                     "POST %s HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n\r\n%s",
                     path, host, port, (int)strlen(body), body);
    return n < 0 || (size_t)n >= cap ? 0 : (size_t)n;
}

/* "voice":"..","speed":".." for whichever are set; returns the length. */
static size_t voice_kv(char *out, size_t cap, const char *voice, const char *speed)
{
    char e[160];
    size_t n = 0;
    if(voice) {
        json_esc(e, sizeof e, voice);
        n += (size_t)snprintf(out + n, cap - n, "\"voice\":\"%s\"", e);
    }
    if(speed) {
        json_esc(e, sizeof e, speed);
        n += (size_t)snprintf(out + n, cap - n, "%s\"speed\":\"%s\"", n ? "," : "", e);
    }
    return n;
}

size_t live_tts_request(char *out, size_t cap, const char *host, int port, const char *voice, const char *speed,
                        const char *avatar)
{
    char body[480], kv[400], av[64];
    size_t bl = 0;
    body[0] = '\0';
    if(voice || speed) {
        voice_kv(kv, sizeof kv, voice, speed);
        bl += (size_t)snprintf(body, sizeof body, "{\"tts\":{%s}", kv);
    }
    if(avatar) {
        json_esc(av, sizeof av, avatar);
        bl += (size_t)snprintf(body + bl, sizeof body - bl, "%s\"avatar\":\"%s\"", bl ? "," : "{", av);
    }
    if(!bl) snprintf(body, sizeof body, "{");
    snprintf(body + strlen(body), sizeof body - strlen(body), "}");
    return post_json(out, cap, "/v1/settings", host, port, body);
}

size_t live_preview_request(char *out, size_t cap, const char *host, int port, const char *voice, const char *speed)
{
    char body[480], kv[400];
    voice_kv(kv, sizeof kv, voice, speed);
    snprintf(body, sizeof body, "{%s}", kv);
    return post_json(out, cap, "/v1/settings/preview", host, port, body);
}

bool live_tts_fetch(live_tts_t *out, char *err, size_t err_cap)
{
    char req[256];
    int n = snprintf(req, sizeof req, "GET /v1/settings HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n",
                     L.host, L.port);
    cbj_t *j = http_json(req, (size_t)n, 900, err, err_cap);
    bool ok = j && tts_from(j, out);
    if(j && !ok) snprintf(err, err_cap, "this Nova pipeline is too old for that");
    cbj_free(j);
    return ok;
}

bool live_tts_apply(const char *voice, const char *speed, const char *avatar, live_tts_t *now, char *err, size_t err_cap)
{
    char req[1024];
    size_t n = live_tts_request(req, sizeof req, L.host, L.port, voice, speed, avatar);
    cbj_t *j = http_json(req, n, 20000, err, err_cap);   /* a new voice loads first */
    bool ok = j && cbj_bool(cbj_get(j, "ok"), false);
    if(now) {
        memset(now, 0, sizeof *now);
        if(j) tts_from(cbj_get(j, "settings"), now);   /* sent on failure too */
    }
    if(j && !ok) snprintf(err, err_cap, "%s", cbj_str(cbj_get(j, "error"), "rejected"));
    cbj_free(j);
    return ok;
}

bool live_tts_preview(const char *voice, const char *speed, char *err, size_t err_cap)
{
    char req[1024];
    size_t n = live_preview_request(req, sizeof req, L.host, L.port, voice, speed);
    cbj_t *j = http_json(req, n, 20000, err, err_cap);
    bool ok = j && cbj_bool(cbj_get(j, "ok"), false);
    if(j && !ok) snprintf(err, err_cap, "%s", cbj_str(cbj_get(j, "error"), "rejected"));
    cbj_free(j);
    return ok;
}

/* ---------- mic and turn control ---------- */

size_t live_session_request(char *out, size_t cap, const char *host, int port, live_session_op_t op, bool muted)
{
    static const char *const path[] = { [LIVE_SESSION_MUTE] = "mute", [LIVE_SESSION_ARM] = "arm",
                                        [LIVE_SESSION_CANCEL] = "cancel" };
    char body[64] = "{}";
    if(op == LIVE_SESSION_MUTE)
        snprintf(body, sizeof body, "{\"muted\":%s,\"source\":\"cockpit\"}", muted ? "true" : "false");
    int n = snprintf(out, cap,
                     "POST /v1/session/%s HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n\r\n%s",
                     path[op], host, port, (int)strlen(body), body);
    return n < 0 || (size_t)n >= cap ? 0 : (size_t)n;
}

static const char *session_reason(const char *code)
{
    if(!strcmp(code, "mic_muted")) return "the mic is muted";
    if(!strcmp(code, "no_session")) return "no voice client is connected";
    return code;
}

bool live_session_post(live_session_op_t op, bool muted, bool *muted_now, char *err, size_t err_cap)
{
    char req[512];
    size_t n = live_session_request(req, sizeof req, L.host, L.port, op, muted);
    cbj_t *j = http_json(req, n, 1200, err, err_cap);
    bool ok = j && cbj_bool(cbj_get(j, "ok"), false);
    const cbj_t *m = j ? cbj_get(j, "muted") : NULL;
    if(muted_now && m && m->type == CBJ_BOOL) *muted_now = m->b;
    if(j && !ok) snprintf(err, err_cap, "%s", session_reason(cbj_str(cbj_get(j, "error"), "rejected")));
    cbj_free(j);
    return ok;
}

/* ---------- touch writes ---------- */

size_t live_set_request(char *out, size_t cap, const char *host, int port, const char *vss_path, double value)
{
    char body[256];
    int bl = snprintf(body, sizeof body, "{\"path\":\"%s\",\"value\":%g}", vss_path, value);
    int n = snprintf(out, cap,
                     "POST /v1/vehicle/set HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n\r\n%s",
                     host, port, bl, body);
    return n < 0 || (size_t)n >= cap ? 0 : (size_t)n;
}

/* Loopback POST with bounded waits, so a stuck server cannot hold the UI for long. */
static bool post_set(const char *vss_path, double value, char *err, size_t err_cap)
{
    char req[640], resp[2048];
    size_t n = live_set_request(req, sizeof req, L.host, L.port, vss_path, value);
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM }, *ai = NULL;
    char port[8];
    snprintf(port, sizeof port, "%d", L.port);
    if(!n || getaddrinfo(L.host, port, &hints, &ai) != 0 || !ai) {
        snprintf(err, err_cap, "no route to Nova");
        return false;
    }
    int fd = socket(ai->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int r = connect(fd, ai->ai_addr, ai->ai_addrlen);
    freeaddrinfo(ai);
    struct pollfd p = { fd, POLLOUT, 0 };
    if((r != 0 && errno != EINPROGRESS) || poll(&p, 1, 300) <= 0 || send(fd, req, n, MSG_NOSIGNAL) != (ssize_t)n) {
        close(fd);
        snprintf(err, err_cap, "Nova is not reachable");
        return false;
    }
    size_t got = 0;
    p.events = POLLIN;
    while(got < sizeof resp - 1 && poll(&p, 1, 600) > 0) {
        ssize_t k = recv(fd, resp + got, sizeof resp - 1 - got, 0);
        if(k <= 0) break;
        got += (size_t)k;
    }
    close(fd);
    resp[got] = '\0';
    const char *body = strstr(resp, "\r\n\r\n");
    cbj_t *j = body ? cbj_parse(body + 4, strlen(body + 4)) : NULL;
    bool ok = j && cbj_bool(cbj_get(j, "ok"), false);
    if(!ok) snprintf(err, err_cap, "%s", j ? cbj_str(cbj_get(j, "error"), "rejected") : "no answer from Nova");
    cbj_free(j);
    return ok;
}

void live_temp_step(bool driver, int32_t delta_dc)
{
    cb_model_t *m = cb_model();
    int32_t now = lv_subject_get_int(driver ? m->driver_temp_dc : m->passenger_temp_dc);
    int32_t want = now + delta_dc;
    if(want < 160) want = 160;
    if(want > 300) want = 300;
    cb_model_set_temp(driver, want);   /* optimistic; the next vehicle delta confirms or corrects it */
    if(!L.ws) return;
    char err[96];
    const char *path = driver ? "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature"
                              : "Vehicle.Cabin.HVAC.Station.Row1.Passenger.Temperature";
    if(post_set(path, want / 10.0, err, sizeof err)) return;
    cb_model_set_temp(driver, now);
    fprintf(stderr, "live: temperature not set: %s\n", err);
    live_toast("Couldn't change the temperature", err);
}
