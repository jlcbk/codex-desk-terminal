/**
 * @file main.c
 * codex-display-sim 模拟器（P1.3 骨架 + P2.1 状态注入/渲染/抓帧，A2）。
 *
 * P2.1 新增（宿主行为只在本文件，shared/ui 不做 IO）：
 *   --state <file.json>      启动读入 AppState JSON（shared/state 解析器；
 *                            坏文件/非法 JSON 报错并以非 0 退出）
 *   --battery-mv N           合成 DeviceRuntime 电池电压；
 *                            usable_percent = clamp((mv-3600)/600*100)（§7.1）
 *   --link-state <s>         connected | stale | disconnected（默认 connected）
 *   --capture-frame out.bmp  退出前把 400×300 逻辑帧（经 cdt_frame_t 公共单色
 *                            格式，1=黑）存 1bpp BMP——供 P2.4 golden 流使用；
 *                            不是 SDL 窗口截图
 *   --quit-after-ms N        N 毫秒后自动退出（等价旧环境变量 SIM_AUTO_QUIT_MS）
 *
 * 保留 P1.3 行为：无 --state 时渲染占位画面；SIM_CAPTURE_PATH 仍存 SDL 帧
 * （人工证据）；Esc/窗口关闭退出；Space/Right 打印 KEY 事件。
 *
 * 键盘映射：
 *   Space / Right  -> KEY 短按（转发 cdt_ui_key 骨架并打印；导航 P2.2）
 *   Esc            -> 退出
 *   窗口关闭按钮    -> LVGL LV_SDL_DIRECT_EXIT 路径（SDL_Quit + lv_deinit + exit(0)）
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cdt_frame.h"
#include "cdt_parser.h"
#include "cdt_presenter.h"
#include "cdt_ui.h"
#include "lvgl.h"
#include LV_SDL_INCLUDE_PATH

#define SIM_HOR_RES 400
#define SIM_VER_RES 300

static volatile int g_quit_requested = 0;

/*---------- 启动选项 ----------*/

typedef struct {
    const char *state_path;     /* --state */
    long battery_mv;            /* --battery-mv（<0 = 未指定） */
    const char *link_state;     /* --link-state */
    const char *capture_frame;  /* --capture-frame（逻辑单色 BMP） */
    long quit_after_ms;         /* --quit-after-ms（<0 = 未指定） */
    long fixed_clock_ms;        /* --fixed-clock（<0 = 未指定；>0 时虚拟单调时钟恒定） */
} sim_opts_t;

static void usage(const char *prog)
{
    printf("usage: %s [options]\n"
           "  --state <file.json>          AppState JSON (shared/state parser; bad file -> exit != 0)\n"
           "  --battery-mv <N>             battery millivolts (default 3900)\n"
           "  --link-state <connected|stale|disconnected>  (default connected)\n"
           "  --capture-frame <out.bmp>    save logical 400x300 1bpp mono frame (1=black) on exit\n"
           "  --quit-after-ms <N>          auto quit after N ms (env SIM_AUTO_QUIT_MS still honored)\n"
           "  --fixed-clock <ms>           freeze monotonic clock at <ms> for deterministic capture (P2.4)\n"
           "Env: SIM_AUTO_QUIT_MS, SIM_CAPTURE_PATH (legacy SDL-frame evidence)\n",
           prog);
}

static void parse_args(int argc, char **argv, sim_opts_t *o)
{
    int i;
    memset(o, 0, sizeof(*o));
    o->battery_mv = -1;
    o->quit_after_ms = -1;
    o->fixed_clock_ms = -1;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "--state") == 0 && i + 1 < argc) {
            o->state_path = argv[++i];
        }
        else if (strcmp(a, "--battery-mv") == 0 && i + 1 < argc) {
            o->battery_mv = atol(argv[++i]);
        }
        else if (strcmp(a, "--link-state") == 0 && i + 1 < argc) {
            o->link_state = argv[++i];
        }
        else if (strcmp(a, "--capture-frame") == 0 && i + 1 < argc) {
            o->capture_frame = argv[++i];
        }
        else if (strcmp(a, "--quit-after-ms") == 0 && i + 1 < argc) {
            o->quit_after_ms = atol(argv[++i]);
        }
        else if (strcmp(a, "--fixed-clock") == 0 && i + 1 < argc) {
            o->fixed_clock_ms = atol(argv[++i]);
        }
        else if (strcmp(a, "--help") == 0 || strcmp(a, "-h") == 0) {
            usage(argv[0]);
            exit(0);
        }
        else {
            fprintf(stderr, "[sim] ERROR: unknown/incomplete argument: %s\n", a);
            usage(argv[0]);
            exit(2);
        }
    }

    /* 旧环境变量兜底（P1.3 兼容）*/
    if (o->quit_after_ms < 0) {
        const char *env = getenv("SIM_AUTO_QUIT_MS");
        o->quit_after_ms = env ? atol(env) : 0;
    }
}

/*---------- --state：宿主侧读文件 + shared/state 有界解析（坏包报错退出非 0）----------*/

static int load_state_file(const char *path, cdt_app_state_t *out)
{
    FILE *f;
    unsigned char *buf;
    long n;
    cdt_parse_result_t r;

    f = fopen(path, "rb");
    if (f == NULL) {
        fprintf(stderr, "[sim] ERROR: cannot open state file '%s'\n", path);
        return -1;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (n = ftell(f)) < 0) {
        fprintf(stderr, "[sim] ERROR: cannot stat state file '%s'\n", path);
        fclose(f);
        return -1;
    }
    fseek(f, 0, SEEK_SET);
    if (n > CDT_STATE_JSON_MAX_BYTES) {
        fprintf(stderr, "[sim] ERROR: state file '%s' is %ld bytes > protocol limit %d\n",
                path, n, CDT_STATE_JSON_MAX_BYTES);
        fclose(f);
        return -1;
    }
    buf = (unsigned char *)malloc((size_t)n + 1);
    if (buf == NULL || fread(buf, 1, (size_t)n, f) != (size_t)n) {
        fprintf(stderr, "[sim] ERROR: cannot read state file '%s'\n", path);
        free(buf);
        fclose(f);
        return -1;
    }
    fclose(f);

    r = cdt_state_parse(buf, (size_t)n, out);
    free(buf);
    if (r != CDT_PARSE_OK) {
        fprintf(stderr, "[sim] ERROR: state file '%s' rejected by parser (code %d: "
                "2=version 3=size 4=depth 5=field 6=utf8 7=enum)\n", path, (int)r);
        return -1;
    }
    return 0;
}

/*---------- DeviceRuntime 合成（模拟器测试注入；生产固件不经此路径）----------*/

/* 虚拟单调时钟：--fixed-clock N 时恒为 N（golden 抓帧确定性）；否则真实节拍 */
static const sim_opts_t *g_opts; /* main 初始化后只读 */
static uint32_t sim_now_ms(void)
{
    if (g_opts != NULL && g_opts->fixed_clock_ms > 0) {
        return (uint32_t)g_opts->fixed_clock_ms;
    }
    return SDL_GetTicks();
}

static uint8_t usable_percent_from_mv(long mv)
{
    long pct;
    if (mv <= 3600) return 0;
    if (mv >= 4200) return 100;
    pct = (mv - 3600) * 100 / 600; /* §7.1 线性估算 clamp((mv-3600)/600*100) */
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    return (uint8_t)pct;
}

static void synth_runtime(const sim_opts_t *o, cdt_runtime_t *rt)
{
    long mv = (o->battery_mv >= 0) ? o->battery_mv : 3900;

    memset(rt, 0, sizeof(*rt));
    rt->battery_valid = (mv > 0 && mv <= 65535);
    rt->battery_mv = rt->battery_valid ? (uint16_t)mv : 0;
    rt->usable_percent = rt->battery_valid ? usable_percent_from_mv(mv) : 0;
    rt->charging = CDT_PRESENCE_UNKNOWN;   /* 不根据高电压猜充电 */
    rt->external_power = CDT_PRESENCE_UNKNOWN;
    rt->power_state = CDT_POWER_ACTIVE;    /* 低压强制页经 Power FSM（P5.1），不在此伪造 */
    snprintf(rt->transport, sizeof(rt->transport), "mock");
    if (o->link_state == NULL || strcmp(o->link_state, "connected") == 0) {
        rt->link_state = CDT_LINK_CONNECTED;
    }
    else if (strcmp(o->link_state, "stale") == 0) {
        rt->link_state = CDT_LINK_STALE;
    }
    else if (strcmp(o->link_state, "disconnected") == 0) {
        rt->link_state = CDT_LINK_DISCONNECTED;
    }
    else {
        fprintf(stderr, "[sim] ERROR: --link-state must be connected|stale|disconnected\n");
        exit(2);
    }
    /* 快照视为进程启动时刻收到：last_rx=0，fresh 时时长随单调时间推进 */
    rt->last_rx_monotonic_ms = 0;
    rt->selected_page = CDT_PAGE_NOW; /* P2.1 仅 NOW 单页 */
}

/*---------- 逻辑单色帧抓取（P2.4 golden 流入口）----------
 * 渲染后从 SDL 渲染器回读像素（内容即 LVGL I1 → 黑/白两色），按公共单色帧
 * cdt_frame_t（400×300、1bpp、MSB=左、1=黑）重新打包，再写 1bpp BMP。
 * 绝不把 SDL 截图冒充逻辑帧。 */

static void sample_into_frame(const uint8_t *argb, int ow, int oh, cdt_frame_t *f)
{
    int x, y;

    cdt_frame_clear(f, 0);
    for (y = 0; y < SIM_VER_RES; y++) {
        int sy = (int)((long)y * oh / SIM_VER_RES);
        const uint32_t *row = (const uint32_t *)(argb + (size_t)sy * ow * 4);
        for (x = 0; x < SIM_HOR_RES; x++) {
            int sx = (int)((long)x * ow / SIM_HOR_RES);
            uint32_t px = row[sx];
            /* I1 渲染为纯黑 0xFF000000 / 纯白 0xFFFFFFFF；阈值防潜在缩放灰阶 */
            int black = ((px & 0xFFu) < 0x80u);
            if (black) cdt_frame_set(f, x, y, 1);
        }
    }
}

static int capture_logical_frame(lv_display_t *disp, cdt_frame_t *f)
{
    SDL_Renderer *ren;
    SDL_Surface *surf;
    int ow, oh;

    lv_refr_now(disp); /* 确保待抓帧包含最后一次 apply（已应用且无脏区时为空操作） */

    ren = (SDL_Renderer *)lv_sdl_window_get_renderer(disp);
    if (ren == NULL) {
        fprintf(stderr, "[sim] capture-frame: no SDL renderer\n");
        return -1;
    }
    if (SDL_GetRendererOutputSize(ren, &ow, &oh) != 0) {
        fprintf(stderr, "[sim] capture-frame: GetRendererOutputSize failed: %s\n", SDL_GetError());
        return -1;
    }
    surf = SDL_CreateRGBSurfaceWithFormat(0, ow, oh, 32, SDL_PIXELFORMAT_ARGB8888);
    if (surf == NULL) {
        fprintf(stderr, "[sim] capture-frame: surface alloc failed: %s\n", SDL_GetError());
        return -1;
    }
    if (SDL_RenderReadPixels(ren, NULL, SDL_PIXELFORMAT_ARGB8888,
                             surf->pixels, surf->pitch) != 0) {
        fprintf(stderr, "[sim] capture-frame: RenderReadPixels failed: %s\n", SDL_GetError());
        SDL_FreeSurface(surf);
        return -1;
    }
    sample_into_frame((const uint8_t *)surf->pixels, ow, oh, f);

    SDL_FreeSurface(surf);
    return 0;
}

/* 小端写字节序辅助（BMP 头） */
static void put_u16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

/* 1bpp BMP：行按 4 字节对齐（400bit=50B→52B），自底向上，MSB=左像素；
 * 调色板 0=白 1=黑，与 cdt_frame_t 的 1=黑 一致。*/
static int write_frame_bmp(const cdt_frame_t *f, const char *path)
{
    const int row_bytes = 52;
    const int data_size = row_bytes * SIM_VER_RES;
    const int file_size = 14 + 40 + 8 + data_size;
    uint8_t hdr[54];
    uint8_t row[52];
    FILE *fp;
    int x, y;

    memset(hdr, 0, sizeof(hdr));
    hdr[0] = 'B'; hdr[1] = 'M';
    put_u32(&hdr[2], (uint32_t)file_size);   /* 文件大小 */
    put_u32(&hdr[10], 54u + 8u);             /* 像素数据偏移 = 14 + 40 + 8(调色板) */
    put_u32(&hdr[14], 40u);                  /* BITMAPINFOHEADER.biSize */
    put_u32(&hdr[18], (uint32_t)SIM_HOR_RES);
    put_u32(&hdr[22], (uint32_t)SIM_VER_RES); /* 正值 = 自底向上 */
    put_u16(&hdr[26], 1u);                   /* planes */
    put_u16(&hdr[28], 1u);                   /* bitCount = 1bpp */
    /* hdr[30..33] biCompression = 0（BI_RGB，memset 已清零） */
    put_u32(&hdr[34], (uint32_t)data_size);  /* biSizeImage */
    put_u32(&hdr[46], 2u);                   /* biClrUsed */
    put_u32(&hdr[50], 2u);                   /* biClrImportant */

    fp = fopen(path, "wb");
    if (fp == NULL) {
        fprintf(stderr, "[sim] capture-frame: cannot open '%s' for write\n", path);
        return -1;
    }
    fwrite(hdr, 1, sizeof(hdr), fp);
    /* 调色板：索引 0 = 白，索引 1 = 黑（BGRA） */
    fputc(255, fp); fputc(255, fp); fputc(255, fp); fputc(0, fp);
    fputc(0, fp); fputc(0, fp); fputc(0, fp); fputc(0, fp);

    for (y = SIM_VER_RES - 1; y >= 0; y--) {
        memset(row, 0, sizeof(row));
        for (x = 0; x < SIM_HOR_RES; x++) {
            if (cdt_frame_get(f, x, y) == 1) {
                row[x >> 3] |= (uint8_t)(0x80u >> (x & 7));
            }
        }
        fwrite(row, 1, (size_t)row_bytes, fp);
    }
    fclose(fp);
    return 0;
}

/*---------- SDL 事件观察（不消费事件，LVGL 的 SDL 驱动仍照常轮询） ----------*/

static void key_event_skeleton(const char *sdl_name, const char *key_sem)
{
    /* KEY 事件骨架：打印并转发共享 UI 桩（P2.2 接页面导航/静音）。 */
    printf("[sim] KEY event: %s (SDL: %s)\n", key_sem, sdl_name);
    fflush(stdout);
    cdt_ui_key(strcmp(key_sem, "long_press") == 0 ? CDT_KEY_LONG_PRESS : CDT_KEY_SHORT_PRESS);
}

static int SDLCALL sim_event_watch(void *userdata, SDL_Event *event)
{
    (void)userdata;
    switch(event->type) {
        case SDL_QUIT:
            printf("[sim] SDL_QUIT received (window close)\n");
            fflush(stdout);
            g_quit_requested = 1;
            break;
        case SDL_KEYDOWN:
            switch(event->key.keysym.sym) {
                case SDLK_ESCAPE:
                    printf("[sim] ESC pressed -> quit\n");
                    fflush(stdout);
                    g_quit_requested = 1;
                    break;
                case SDLK_SPACE:
                    key_event_skeleton("SPACE", "short_press");
                    break;
                case SDLK_RIGHT:
                    key_event_skeleton("RIGHT", "short_press");
                    break;
                default:
                    break;
            }
            break;
        default:
            break;
    }
    return 0; /* 不修改/拦截事件，LVGL 的 sdl_event_handler 照常处理 */
}

/*---------- 退出前抓帧（P1.3 旧行为：SDL 帧证据；窗口截图替代品） ----------*/

static void sim_capture_sdl_frame(lv_display_t *disp, const char *path)
{
    SDL_Renderer *ren;
    SDL_Surface *surf;
    int w, h;

    if(path == NULL || path[0] == '\0') return;

    ren = (SDL_Renderer *)lv_sdl_window_get_renderer(disp);
    if(ren == NULL) {
        printf("[sim] capture: no renderer, skip\n");
        fflush(stdout);
        return;
    }
    if(SDL_GetRendererOutputSize(ren, &w, &h) != 0) {
        printf("[sim] capture: GetRendererOutputSize failed: %s\n", SDL_GetError());
        fflush(stdout);
        return;
    }
    surf = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, SDL_PIXELFORMAT_ARGB8888);
    if(surf == NULL) {
        printf("[sim] capture: surface alloc failed: %s\n", SDL_GetError());
        fflush(stdout);
        return;
    }
    if(SDL_RenderReadPixels(ren, NULL, SDL_PIXELFORMAT_ARGB8888, surf->pixels, surf->pitch) != 0) {
        printf("[sim] capture: RenderReadPixels failed: %s\n", SDL_GetError());
        SDL_FreeSurface(surf);
        return;
    }
    if(SDL_SaveBMP(surf, path) == 0) {
        printf("[sim] SDL frame captured: %s (%dx%d)\n", path, w, h);
    }
    else {
        printf("[sim] capture: SaveBMP failed: %s\n", SDL_GetError());
    }
    fflush(stdout);
    SDL_FreeSurface(surf);
}

/*---------- 受控退出（Esc / 自动退出路径；窗口关闭由 LVGL 直接 exit(0)） ----------*/

static void sim_shutdown(lv_display_t *disp, const sim_opts_t *o, const char *sdl_capture_path)
{
    /* 逻辑单色帧必须先于 display 销毁抓取 */
    if (o->capture_frame != NULL) {
        cdt_frame_t f;
        if (capture_logical_frame(disp, &f) == 0 &&
            write_frame_bmp(&f, o->capture_frame) == 0) {
            printf("[sim] logical mono frame captured: %s (%dx%d, 1bpp, 1=black, %d bytes)\n",
                   o->capture_frame, CDT_FRAME_WIDTH, CDT_FRAME_HEIGHT, CDT_FRAME_BYTES);
        }
        else {
            fprintf(stderr, "[sim] capture-frame FAILED for '%s'\n", o->capture_frame);
            exit(3);
        }
    }
    sim_capture_sdl_frame(disp, sdl_capture_path);

    lv_display_delete(disp);   /* 触发 release_disp_cb：销毁 texture/renderer/window，释放绘制缓冲 */
    lv_sdl_quit();             /* SDL_Quit() + 删除 LVGL 的 SDL 事件轮询定时器 */
    lv_deinit();               /* 释放 LVGL 全局状态与内存池 */
}

/*---------- 占位画面（无 --state 时保留 P1.3 行为） ----------*/

static void sim_build_placeholder_ui(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_t *rect;
    lv_obj_t *label;
    lv_obj_t *hint;

    rect = lv_obj_create(scr);
    lv_obj_remove_style_all(rect);
    lv_obj_set_size(rect, 96, 48);
    lv_obj_set_pos(rect, 16, 16);
    lv_obj_set_style_bg_color(rect, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(rect, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_color(rect, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_border_width(rect, 2, LV_PART_MAIN);
    lv_obj_set_style_radius(rect, 0, LV_PART_MAIN);

    label = lv_label_create(scr);
    lv_label_set_text(label, "codex-desk-terminal sim");
    lv_obj_set_style_text_color(label, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_center(label);

    hint = lv_label_create(scr);
    lv_label_set_text(hint, "Space/Right=KEY  Esc=quit");
    lv_obj_set_style_text_color(hint, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_text_font(hint, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_align(hint, LV_ALIGN_CENTER, 0, 24);
}

int main(int argc, char **argv)
{
    sim_opts_t opts;
    const char *sdl_capture_env = getenv("SIM_CAPTURE_PATH");
    uint32_t t0, now, last_heartbeat, last_present;
    lv_display_t *disp;
    cdt_app_state_t app_state;
    cdt_runtime_t runtime;
    const cdt_app_state_t *state_ptr = NULL;
    cdt_view_t view;

    parse_args(argc, argv, &opts);

    /* --state 文件先于 SDL/LVGL 初始化解析：坏文件直接非 0 退出 */
    if (opts.state_path != NULL) {
        if (load_state_file(opts.state_path, &app_state) != 0) {
            return 2;
        }
        state_ptr = &app_state;
    }
    g_opts = &opts;
    synth_runtime(&opts, &runtime);

    printf("[sim] codex-display-sim (P2.1) LVGL %d.%d.%d, %dx%d, LV_COLOR_DEPTH=%d\n",
           lv_version_major(), lv_version_minor(), lv_version_patch(),
           SIM_HOR_RES, SIM_VER_RES, LV_COLOR_DEPTH);
    if (state_ptr != NULL) {
        printf("[sim] state: %s (seq=%llu, threads=%u, epoch=%.16s) battery=%umV link=%s\n",
               opts.state_path, (unsigned long long)state_ptr->seq,
               (unsigned)state_ptr->thread_count, state_ptr->bridge_epoch,
               (unsigned)runtime.battery_mv,
               runtime.link_state == CDT_LINK_CONNECTED ? "connected" :
               runtime.link_state == CDT_LINK_STALE ? "stale" : "disconnected");
    }
    else {
        printf("[sim] no --state given: P1.3 placeholder UI\n");
    }
    fflush(stdout);

    /* LVGL 初始化（lv_sdl_window_create 内部也会 SDL_Init，幂等） */
    lv_init();
    if(SDL_Init(SDL_INIT_VIDEO) != 0) {
        printf("[sim] SDL_Init failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_AddEventWatch(sim_event_watch, NULL);

    disp = lv_sdl_window_create(SIM_HOR_RES, SIM_VER_RES);
    if(disp == NULL) {
        printf("[sim] lv_sdl_window_create failed\n");
        SDL_Quit();
        return 1;
    }
    lv_sdl_window_set_title(disp, "codex-desk-terminal sim");
    lv_sdl_window_set_resizeable(disp, false);

    if (state_ptr != NULL) {
        cdt_ui_init();
        cdt_present(state_ptr, &runtime, sim_now_ms(), &view);
        cdt_ui_apply(&view);
    }
    else {
        sim_build_placeholder_ui();
    }
    /* 整帧刷新：与 v1 显示模型一致——flush 支持整帧，dirty_area 仅是后续优化
     * 提示（INTERFACES §5）；模拟器每次刷新整屏，保证逻辑帧确定性（P2.4 golden
     * 流依赖逐像素可复现）。 */
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(disp);

    t0 = SDL_GetTicks();
    last_heartbeat = t0;
    last_present = t0;
    printf("[sim] entering main loop%s\n",
           opts.quit_after_ms > 0 ? " (auto-quit)" : "");
    fflush(stdout);

    while(!g_quit_requested) {
        uint32_t next = lv_timer_handler();

        now = SDL_GetTicks();
        if(opts.quit_after_ms > 0 && (now - t0) >= (uint32_t)opts.quit_after_ms) {
            printf("[sim] auto-quit after %lums\n", (unsigned long)(now - t0));
            fflush(stdout);
            break;
        }

        /* 周期 present+apply：模拟器宿主节拍（真机由 DeviceRuntime 决定刷新节拍；
         * 链路 fresh 时 elapsed 随单调时间增长，陈旧时 presenter 冻结）。
         * apply 后整屏失效 → 单个 400px 宽刷新区，规避 SDL I1 局部 stride 缺陷。 */
        if (state_ptr != NULL && (now - last_present) >= 200) {
            last_present = now;
            cdt_present(state_ptr, &runtime, sim_now_ms(), &view);
            cdt_ui_apply(&view);
            lv_obj_invalidate(lv_screen_active());
        }

        if(now - last_heartbeat >= 1000) {
            printf("[sim] alive, tick=%lums\n", (unsigned long)now);
            fflush(stdout);
            last_heartbeat = now;
        }

        if(next > 30) next = 30;    /* 封顶，保证退出检查响应性 */
        SDL_Delay(next ? next : 1);
    }

    printf("[sim] shutting down\n");
    fflush(stdout);
    sim_shutdown(disp, &opts, sdl_capture_env);
    printf("[sim] clean exit\n");
    fflush(stdout);
    return 0;
}
