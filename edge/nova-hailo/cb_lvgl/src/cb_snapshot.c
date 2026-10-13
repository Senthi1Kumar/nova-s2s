#include "cb_snapshot.h"
#include <stdio.h>

bool cb_snapshot_ppm(lv_obj_t *obj, const char *path)
{
    lv_draw_buf_t *buf = lv_snapshot_take(obj, LV_COLOR_FORMAT_XRGB8888);
    if(!buf) return false;
    FILE *f = fopen(path, "wb");
    if(!f) {
        lv_draw_buf_destroy(buf);
        return false;
    }
    uint32_t w = buf->header.w, h = buf->header.h, stride = buf->header.stride;
    fprintf(f, "P6\n%u %u\n255\n", (unsigned)w, (unsigned)h);
    for(uint32_t y = 0; y < h; y++) {
        const uint8_t *row = buf->data + (size_t)y * stride;
        for(uint32_t x = 0; x < w; x++) {
            const uint8_t px[3] = { row[x * 4 + 2], row[x * 4 + 1], row[x * 4 + 0] };   /* BGRX -> RGB */
            fwrite(px, 1, 3, f);
        }
    }
    fclose(f);
    lv_draw_buf_destroy(buf);
    return true;
}

typedef struct { lv_obj_t *obj; const char *path; } snap_job_t;

static void snapshot_timer_cb(lv_timer_t *t)
{
    snap_job_t *job = lv_timer_get_user_data(t);
    bool ok = cb_snapshot_ppm(job->obj, job->path);
    printf("snapshot %s %s\n", job->path, ok ? "written" : "FAILED");
    fflush(stdout);
    lv_free(job);
}

void cb_snapshot_after(lv_obj_t *obj, const char *path, uint32_t delay_ms)
{
    snap_job_t *job = lv_malloc(sizeof(*job));
    if(!job) return;
    *job = (snap_job_t){ obj, path };
    lv_timer_t *t = lv_timer_create(snapshot_timer_cb, delay_ms, job);
    lv_timer_set_repeat_count(t, 1);
}
