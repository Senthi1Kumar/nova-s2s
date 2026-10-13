/* Map spike probe: renders a style off-screen through map_engine, saves the settled frame, then measures a
   drag the way the map page will drive it (one move_by per frame). Prints one summary line per phase:
     unpaced  -> the engine's ceiling (render + readback + BGRA swap per frame)
     paced    -> CPU at the target frame rate, all threads (tile parsing included)
   Usage: map_probe [--style URL] [--size WxH] [--center LAT,LON] [--zoom Z] [--frames N] [--pace FPS]
                    [--seconds S] [--token-env NAME] [--out PREFIX] */
#define _GNU_SOURCE   /* RUSAGE_THREAD */
#include "map/map_engine.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static double cpu_ms(void)
{
    struct rusage r;
    getrusage(RUSAGE_SELF, &r);
    return (r.ru_utime.tv_sec + r.ru_stime.tv_sec) * 1e3 + (r.ru_utime.tv_usec + r.ru_stime.tv_usec) / 1e3;
}

static double thread_cpu_ms(void)
{
    struct rusage r;
    getrusage(RUSAGE_THREAD, &r);
    return (r.ru_utime.tv_sec + r.ru_stime.tv_sec) * 1e3 + (r.ru_utime.tv_usec + r.ru_stime.tv_usec) / 1e3;
}

static void print_stage(const char *phase, const cb_map_stats_t *a, const cb_map_stats_t *b, double wall, double cpu,
                        double main_cpu)
{
    uint32_t n = b->frames - a->frames;
    if(n == 0) n = 1;
    printf("probe:   %s per frame: pump %.2f render %.2f read %.2f convert %.2f ms | cpu main %.0f%% workers %.0f%%\n",
           phase, (b->pump_ms - a->pump_ms) / n, (b->render_ms - a->render_ms) / n, (b->read_ms - a->read_ms) / n,
           (b->convert_ms - a->convert_ms) / n, 100.0 * main_cpu / wall, 100.0 * (cpu - main_cpu) / wall);
}

static void sleep_ms(double ms)
{
    if(ms <= 0) return;
    struct timespec t = { (time_t)(ms / 1e3), (long)(fmod(ms, 1e3) * 1e6) };
    nanosleep(&t, NULL);
}

static int cmp_d(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

static bool save_ppm(cb_map_t *m, const char *path)
{
    uint32_t w, h, stride;
    const uint8_t *px = cb_map_pixels(m, &w, &h, &stride);
    if(!px) return false;
    FILE *f = fopen(path, "wb");
    if(!f) return false;
    fprintf(f, "P6\n%u %u\n255\n", w, h);
    for(uint32_t y = 0; y < h; y++) {
        const uint8_t *row = px + (size_t)y * stride;
        for(uint32_t x = 0; x < w; x++) {
            const uint8_t rgb[3] = { row[x * 4 + 2], row[x * 4 + 1], row[x * 4 + 0] };
            fwrite(rgb, 1, 3, f);
        }
    }
    fclose(f);
    return true;
}

/* steps until a frame lands or the wait runs out; returns the time it took, or -1 */
static double next_frame(cb_map_t *m, double max_wait_ms)
{
    double t0 = now_ms();
    for(;;) {
        if(cb_map_step(m)) return now_ms() - t0;
        if(now_ms() - t0 > max_wait_ms) return -1;
        sleep_ms(1);
    }
}

int main(int argc, char **argv)
{
    const char *style = "https://tiles.openfreemap.org/styles/liberty";
    const char *token_env = "MAPBOX_API_KEY";
    const char *out = "map_probe";
    uint32_t w = 1200, h = 760;
    double lat = 12.9716, lon = 77.5946, zoom = 14.5, pace = 30, seconds = 5;
    int frames = 120;
    for(int i = 1; i < argc; i++) {
        const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
        if(!v) break;
        if(!strcmp(a, "--style")) style = v, i++;
        else if(!strcmp(a, "--size")) sscanf(v, "%ux%u", &w, &h), i++;
        else if(!strcmp(a, "--center")) sscanf(v, "%lf,%lf", &lat, &lon), i++;
        else if(!strcmp(a, "--zoom")) zoom = atof(v), i++;
        else if(!strcmp(a, "--frames")) frames = atoi(v), i++;
        else if(!strcmp(a, "--pace")) pace = atof(v), i++;
        else if(!strcmp(a, "--seconds")) seconds = atof(v), i++;
        else if(!strcmp(a, "--token-env")) token_env = v, i++;
        else if(!strcmp(a, "--out")) out = v, i++;
    }
    const char *token = getenv(token_env);
    printf("probe: style=%s size=%ux%u token=%s\n", style, w, h, token && *token ? "set" : "absent");

    double t_create = now_ms();
    cb_map_cfg_t cfg = { .width = w, .height = h, .lat = lat, .lon = lon, .zoom = zoom, .style_url = style,
                         .access_token = token };
    cb_map_t *m = cb_map_create(&cfg);
    if(!m) {
        fprintf(stderr, "probe: create failed: %s\n", cb_map_last_error());
        return 1;
    }
    printf("probe: created in %.0f ms\n", now_ms() - t_create);

    /* load: until frames stop arriving for 1.5 s (tiles, glyphs and fades done) or 25 s */
    double t0 = now_ms(), last = 0, first = -1;
    int load_frames = 0;
    while(now_ms() - t0 < 25000) {
        if(cb_map_step(m)) {
            last = now_ms() - t0;
            if(first < 0) first = last;
            load_frames++;
        }
        if(first >= 0 && now_ms() - t0 - last > 1500) break;
        sleep_ms(2);
    }
    if(first < 0) {
        fprintf(stderr, "probe: no frame in 25 s: %s\n", cb_map_last_error());
        cb_map_destroy(m);
        return 1;
    }
    char path[512];
    snprintf(path, sizeof path, "%s_loaded.ppm", out);
    save_ppm(m, path);
    printf("probe: first frame %.0f ms, settled %.0f ms (%d frames), style_ready=%d -> %s\n", first, last,
           load_frames, cb_map_style_ready(m), path);

    /* unpaced drag: the engine's ceiling */
    double *ft = calloc((size_t)frames, sizeof *ft);
    int got = 0;
    cb_map_gesture(m, true);
    cb_map_stats_t s0, s1;
    cb_map_stats(m, &s0);
    double c0 = cpu_ms(), w0 = now_ms(), mc0 = thread_cpu_ms();
    for(int i = 0; i < frames; i++) {
        cb_map_move_by(m, 6, 2);
        double dt = next_frame(m, 200);
        if(dt >= 0) ft[got++] = dt;
    }
    double wall = now_ms() - w0, cpu = cpu_ms() - c0, mcpu = thread_cpu_ms() - mc0;
    cb_map_stats(m, &s1);
    qsort(ft, (size_t)got, sizeof *ft, cmp_d);
    printf("probe: unpaced drag %d/%d frames, %.1f fps, frame p50 %.1f ms p95 %.1f ms, cpu %.0f%% of one core\n",
           got, frames, got / (wall / 1e3), got ? ft[got / 2] : 0, got ? ft[(int)(got * 0.95)] : 0,
           100.0 * cpu / wall);
    print_stage("unpaced", &s0, &s1, wall, cpu, mcpu);

    /* paced drag at the target rate: what the app would cost */
    double slot = 1e3 / pace;
    int paced = 0, missed = 0;
    cb_map_stats(m, &s0);
    c0 = cpu_ms();
    mc0 = thread_cpu_ms();
    w0 = now_ms();
    double next = w0;
    while(now_ms() - w0 < seconds * 1e3) {
        cb_map_move_by(m, 4, 1.5);
        if(next_frame(m, slot) >= 0) paced++;
        else missed++;
        next += slot;
        sleep_ms(next - now_ms());
    }
    wall = now_ms() - w0;
    cpu = cpu_ms() - c0;
    mcpu = thread_cpu_ms() - mc0;
    cb_map_stats(m, &s1);
    cb_map_gesture(m, false);
    printf("probe: paced drag @%.0f fps for %.1f s: %d frames (%.1f fps), %d missed, cpu %.0f%% of one core\n", pace,
           wall / 1e3, paced, paced / (wall / 1e3), missed, 100.0 * cpu / wall);
    print_stage("paced", &s0, &s1, wall, cpu, mcpu);

    /* zoom in with an eased 2x, then let it settle */
    cb_map_scale_by(m, 2.0, w / 2.0, h / 2.0, 300);
    int zf = 0;
    w0 = now_ms();
    while(now_ms() - w0 < 2500) {
        if(cb_map_step(m)) zf++;
        sleep_ms(2);
    }
    double zl, zlon, zz;
    cb_map_camera(m, &zl, &zlon, &zz, NULL);
    snprintf(path, sizeof path, "%s_after.ppm", out);
    save_ppm(m, path);
    printf("probe: zoom 2x -> z=%.2f (%d frames in 2.5 s) -> %s\n", zz, zf, path);

    free(ft);
    cb_map_destroy(m);
    return 0;
}
