/* Desktop shell for the Nova face: an SDL window (or an in-memory display for
 * tests) driven by UI-state JSON lines on stdin (docs/api/ui-state.md). A tap
 * prints {"wake":true} on stdout for the client.
 *
 *   nova_face [--size WxH] [--round] [--demo] [--script FILE] [--headless]
 */
#define _POSIX_C_SOURCE 200809L

#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "cJSON.h"
#include "lvgl.h"
#include "nova_face.h"

#define QUEUE_LEN 256
#define LINE_MAX_BYTES 65536

/* Lines from the reader thread; the LVGL loop drains them (LVGL is not
 * thread-safe, so only the loop touches widgets). A full queue drops the
 * oldest: every field is a full value, so fresher state always wins. */
static struct {
    char *items[QUEUE_LEN];
    int head, count;
    pthread_mutex_t lock;
} Q = { .lock = PTHREAD_MUTEX_INITIALIZER };
static atomic_int input_done;

static void queue_push(char *line)
{
    pthread_mutex_lock(&Q.lock);
    if (Q.count == QUEUE_LEN) {
        free(Q.items[Q.head]);
        Q.head = (Q.head + 1) % QUEUE_LEN;
        Q.count--;
    }
    Q.items[(Q.head + Q.count) % QUEUE_LEN] = line;
    Q.count++;
    pthread_mutex_unlock(&Q.lock);
}

static char *queue_pop(void)
{
    char *line = NULL;
    pthread_mutex_lock(&Q.lock);
    if (Q.count) {
        line = Q.items[Q.head];
        Q.head = (Q.head + 1) % QUEUE_LEN;
        Q.count--;
    }
    pthread_mutex_unlock(&Q.lock);
    return line;
}

static int queue_len(void)
{
    pthread_mutex_lock(&Q.lock);
    int n = Q.count;
    pthread_mutex_unlock(&Q.lock);
    return n;
}

static void *reader(void *arg)
{
    FILE *in = arg;
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    while ((n = getline(&line, &cap, in)) != -1) {
        if (n > LINE_MAX_BYTES) continue;      /* dropped whole, never cut into bad JSON */
        char *copy = strdup(line);
        if (copy) queue_push(copy);
    }
    free(line);
    input_done = 1;
    return NULL;
}

static int parse_state(const char *s, nova_state_t *out)
{
    static const struct { const char *name; nova_state_t v; } map[] = {
        { "IDLE", NOVA_IDLE }, { "LISTENING", NOVA_LISTENING },
        { "THINKING", NOVA_THINKING }, { "SPEAKING", NOVA_SPEAKING },
    };
    for (size_t i = 0; i < sizeof map / sizeof *map; i++)
        if (strcmp(s, map[i].name) == 0) { *out = map[i].v; return 1; }
    return 0;
}

static void apply_line(const char *line)
{
    cJSON *msg = cJSON_Parse(line);
    if (!cJSON_IsObject(msg)) {                /* garbage and non-objects are ignored */
        cJSON_Delete(msg);
        return;
    }
    const cJSON *v;
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "conn")) && cJSON_IsString(v)) {
        if (strcmp(v->valuestring, "up") == 0) nova_face_set_conn(NOVA_CONN_UP);
        else if (strcmp(v->valuestring, "down") == 0) nova_face_set_conn(NOVA_CONN_DOWN);
        else if (strcmp(v->valuestring, "connecting") == 0) nova_face_set_conn(NOVA_CONN_CONNECTING);
    }
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "state")) && cJSON_IsString(v)) {
        nova_state_t s;
        if (parse_state(v->valuestring, &s)) nova_face_set_state(s);
    }
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "you")) && cJSON_IsString(v))
        nova_face_set_you(v->valuestring);
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "nova")) && cJSON_IsString(v))
        nova_face_set_nova(v->valuestring);
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "level")) && cJSON_IsNumber(v))
        nova_face_set_level((float)v->valuedouble);
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "error")) && cJSON_IsString(v))
        nova_face_set_error(v->valuestring);
    if ((v = cJSON_GetObjectItemCaseSensitive(msg, "_tap")) && cJSON_IsTrue(v))
        nova_face_press_wake();                /* scripts: exercise the wake path */
    cJSON_Delete(msg);
}

static void on_wake(void *user)
{
    (void)user;
    fputs("{\"wake\":true}\n", stdout);
    fflush(stdout);
}

static uint32_t tick_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static void sleep_ms(long ms)
{
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static void flush_nothing(lv_display_t *disp, const lv_area_t *area, uint8_t *px)
{
    (void)area;
    (void)px;
    lv_display_flush_ready(disp);
}

static lv_display_t *headless_display(int32_t w, int32_t h)
{
    lv_display_t *disp = lv_display_create(w, h);
    size_t size = (size_t)w * 40 * 4;          /* 40 rows of 32-bit pixels */
    void *buf = malloc(size);
    if (!disp || !buf) return NULL;
    lv_display_set_buffers(disp, buf, NULL, size, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(disp, flush_nothing);
    return disp;
}

static void demo_tick(lv_timer_t *t)
{
    (void)t;
    static const char *steps[] = {
        "{\"conn\":\"up\"}",
        "{\"state\":\"LISTENING\"}",
        "{\"level\":0.6}",
        "{\"you\":\"What's the weather in Delhi today?\"}",
        "{\"state\":\"THINKING\"}",
        "{\"state\":\"SPEAKING\"}",
        "{\"nova\":\"It's about 33 degrees with a light chance of rain.\"}",
        "{\"state\":\"IDLE\"}",
    };
    static size_t i;
    apply_line(steps[i++ % (sizeof steps / sizeof *steps)]);
}

/* --dump (test/debug only): after the main loop exits, print the widget tree
 * as one JSON object per line, depth-first, then exit. Coordinates are
 * absolute post-layout (lv_obj_get_coords); "hidden" and "overflow" are the
 * object's own flags -- a caller wanting effective visibility walks parents. */
static void dump_obj(lv_obj_t *obj, int id, int parent, int *next_id)
{
    lv_area_t area;
    lv_obj_get_coords(obj, &area);
    const lv_obj_class_t *cls = lv_obj_get_class(obj);
    const char *cls_name = "obj";
    if (cls == &lv_label_class) cls_name = "label";
    else if (cls == &lv_button_class) cls_name = "button";
    else if (cls == &lv_arc_class) cls_name = "arc";

    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "id", id);
    cJSON_AddNumberToObject(o, "parent", parent);
    cJSON_AddStringToObject(o, "cls", cls_name);
    cJSON_AddNumberToObject(o, "x1", area.x1);
    cJSON_AddNumberToObject(o, "y1", area.y1);
    cJSON_AddNumberToObject(o, "x2", area.x2);
    cJSON_AddNumberToObject(o, "y2", area.y2);
    cJSON_AddBoolToObject(o, "hidden", lv_obj_is_hidden(obj));
    cJSON_AddBoolToObject(o, "overflow", lv_obj_is_overflow_visible(obj));
    cJSON_AddNumberToObject(o, "radius", lv_obj_get_style_radius(obj, LV_PART_MAIN));
    if (cls == &lv_label_class) cJSON_AddStringToObject(o, "text", lv_label_get_text(obj));

    char *line = cJSON_PrintUnformatted(o);
    if (line) {
        fputs(line, stdout);
        fputc('\n', stdout);
        cJSON_free(line);
    }
    cJSON_Delete(o);

    uint32_t n = lv_obj_get_child_count(obj);
    for (uint32_t i = 0; i < n; i++) {
        int child_id = (*next_id)++;
        dump_obj(lv_obj_get_child(obj, i), child_id, id, next_id);
    }
}

static int usage(void)
{
    fputs("usage: nova_face [--size WxH] [--round] [--demo] [--script FILE] [--headless] "
          "[--dump]\n", stderr);
    return 2;
}

int main(int argc, char **argv)
{
    int w = 480, h = 480, round_panel = 0, demo = 0, headless = 0, dump = 0;
    const char *script = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--size") == 0 && i + 1 < argc) {
            if (sscanf(argv[++i], "%dx%d", &w, &h) != 2 || w < 120 || h < 120) return usage();
        } else if (strcmp(argv[i], "--round") == 0) {
            round_panel = 1;
        } else if (strcmp(argv[i], "--demo") == 0) {
            demo = 1;
        } else if (strcmp(argv[i], "--script") == 0 && i + 1 < argc) {
            script = argv[++i];
        } else if (strcmp(argv[i], "--headless") == 0) {
            headless = 1;
        } else if (strcmp(argv[i], "--dump") == 0) {
            dump = 1;
        } else {
            return usage();
        }
    }

    lv_init();
    lv_tick_set_cb(tick_ms);
    lv_display_t *disp = headless ? headless_display(w, h) : lv_sdl_window_create(w, h);
    if (!disp) {
        fputs("nova_face: could not open a display\n", stderr);
        return 1;
    }
    if (!headless) lv_sdl_mouse_create();

    lv_obj_t *root = lv_screen_active();
    if (round_panel) {
        /* Preview a round panel: the face lives in a clipped disc. */
        int d = w < h ? w : h;
        lv_obj_set_style_bg_color(root, lv_color_black(), 0);
        lv_obj_t *disc = lv_obj_create(root);
        lv_obj_remove_style_all(disc);
        lv_obj_set_size(disc, d, d);
        lv_obj_center(disc);
        lv_obj_set_style_radius(disc, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_clip_corner(disc, true, 0);
        lv_obj_set_scrollable(disc, false);
        root = disc;
    }
    nova_face_create(root, on_wake, NULL);

    FILE *in = stdin;
    if (script && !(in = fopen(script, "r"))) {
        perror(script);
        return 1;
    }
    pthread_t th;
    pthread_create(&th, NULL, reader, in);
    pthread_detach(th);
    if (demo) lv_timer_create(demo_tick, 2000, NULL);

    int settle = 0;
    for (;;) {
        char *line;
        while ((line = queue_pop())) {
            apply_line(line);
            free(line);
        }
        lv_timer_handler();
        /* Input closed (script done, or the client exited): render a few more
         * frames, then leave. The demo keeps running on its own. */
        if (!demo && input_done && queue_len() == 0 && ++settle > 60) break;
        sleep_ms(5);
    }
    if (dump) {
        lv_obj_update_layout(lv_screen_active());
        int next_id = 1;
        dump_obj(lv_screen_active(), 0, -1, &next_id);
    }
    return 0;
}
