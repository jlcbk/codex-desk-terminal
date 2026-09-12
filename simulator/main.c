/**
 * @file main.c
 * codex-display-sim 模拟器（P1.3 骨架 + P2.1 状态注入/渲染/抓帧
 * + P2.2/P2.3 导航与强制页，A2；P2.4 集成 --scenario 回放，A6）。
 *
 * P2.4 集成新增（宿主行为只在本文件，shared/ui 不做 IO）：
 *   --scenario <file.jsonl>          INTERFACES §4 注入 JSONL 回放（先全量预检
 *                                    再回放；虚拟时钟由 at_ms/advance_time 驱动，
 *                                    确定性渲染）；与 --fixed-clock 互斥
 *   --capture-dir <dir>              帧与 manifest 输出目录（契约
 *                                    tests/UI_CONTRACT.md §3：按场景主名建子目录，
 *                                    每条 action 后 ViewModel 有变化才出帧，帧为
 *                                    400x300 逻辑单色 PNG；manifest 每行
 *                                    frame/scenario/frame_index/at_ms/action/seq
 *                                    + 扩展 view 字段供 check_ui 语义断言）
 *
 * P2.2/P2.3 保留：--page/--battery-seq（经真实 Power FSM）/KEY 导航。
 * P2.1 保留：--state/--battery-mv/--link-state/--capture-frame/--quit-after-ms/
 * --fixed-clock；无 --state 时渲染占位画面；Esc/窗口关闭退出。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

#include "cdt_frame.h"
#include "cdt_json.h"
#include "cdt_nav.h"
#include "cdt_parser.h"
#include "cdt_power.h"
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
    cdt_page_t page;            /* --page（默认 NOW） */
    const char *battery_seq;    /* --battery-seq "mv@ms,..."（FSM 驱动） */
    const char *scenario;       /* --scenario 注入 JSONL（P2.4） */
    const char *capture_dir;    /* --capture-dir（P2.4，帧+manifest） */
} sim_opts_t;

static void usage(const char *prog)
{
    printf("usage: %s [options]\n"
           "  --state <file.json>          AppState JSON (shared/state parser; bad file -> exit != 0)\n"
           "  --battery-mv <N>             battery millivolts (default 3900)\n"
           "  --battery-seq <mv>@<ms>,...  battery samples through the real Power FSM (P5.1)\n"
           "  --link-state <connected|stale|disconnected>  (default connected)\n"
           "  --page <now|agents|plan|usage|details>  initial normal page (default now)\n"
           "  --scenario <file.jsonl>      injection JSONL replay (INTERFACES #4; full\n"
           "                               pre-validation; deterministic virtual clock)\n"
           "  --capture-dir <dir>          frames + manifest.jsonl per scenario subdir (UI_CONTRACT #3)\n"
           "  --capture-frame <out.bmp>    save logical 400x300 1bpp mono frame (1=black) on exit\n"
           "  --quit-after-ms <N>          auto quit after N ms (env SIM_AUTO_QUIT_MS still honored)\n"
           "  --fixed-clock <ms>           freeze monotonic clock at <ms> (single-state capture; not with --scenario)\n"
           "Keys: Space/Right=short press (page cycle), m=long press (mute), Esc=quit\n"
           "Env: SIM_AUTO_QUIT_MS, SIM_CAPTURE_PATH (legacy SDL-frame evidence)\n",
           prog);
}

static cdt_page_t parse_page(const char *s)
{
    if (strcmp(s, "now") == 0) return CDT_PAGE_NOW;
    if (strcmp(s, "agents") == 0) return CDT_PAGE_AGENTS;
    if (strcmp(s, "plan") == 0) return CDT_PAGE_PLAN;
    if (strcmp(s, "usage") == 0) return CDT_PAGE_USAGE;
    if (strcmp(s, "details") == 0) return CDT_PAGE_DETAILS; /* ZC4 v1.2 */
    fprintf(stderr, "[sim] ERROR: --page must be now|agents|plan|usage|details\n");
    exit(2);
}

static void parse_args(int argc, char **argv, sim_opts_t *o)
{
    int i;
    memset(o, 0, sizeof(*o));
    o->battery_mv = -1;
    o->quit_after_ms = -1;
    o->fixed_clock_ms = -1;
    o->page = CDT_PAGE_NOW;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "--state") == 0 && i + 1 < argc) {
            o->state_path = argv[++i];
        }
        else if (strcmp(a, "--battery-mv") == 0 && i + 1 < argc) {
            o->battery_mv = atol(argv[++i]);
        }
        else if (strcmp(a, "--battery-seq") == 0 && i + 1 < argc) {
            o->battery_seq = argv[++i];
        }
        else if (strcmp(a, "--link-state") == 0 && i + 1 < argc) {
            o->link_state = argv[++i];
        }
        else if (strcmp(a, "--page") == 0 && i + 1 < argc) {
            o->page = parse_page(argv[++i]);
        }
        else if (strcmp(a, "--capture-frame") == 0 && i + 1 < argc) {
            o->capture_frame = argv[++i];
        }
        else if (strcmp(a, "--scenario") == 0 && i + 1 < argc) {
            o->scenario = argv[++i];
        }
        else if (strcmp(a, "--capture-dir") == 0 && i + 1 < argc) {
            o->capture_dir = argv[++i];
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

    /* --scenario 与 --fixed-clock/--state/--battery-seq 互斥（回放时钟由 at_ms 驱动）*/
    if (o->scenario != NULL) {
        if (o->fixed_clock_ms > 0) {
            fprintf(stderr, "[sim] ERROR: --scenario drives its own virtual clock; "
                    "--fixed-clock is not applicable\n");
            exit(2);
        }
        if (o->state_path != NULL || o->battery_seq != NULL) {
            fprintf(stderr, "[sim] ERROR: --scenario cannot be combined with "
                    "--state/--battery-seq\n");
            exit(2);
        }
        if (o->capture_dir == NULL) {
            fprintf(stderr, "[sim] ERROR: --scenario requires --capture-dir\n");
            exit(2);
        }
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

/* 虚拟单调时钟：--fixed-clock N 时恒为 N（golden 抓帧确定性）；
 * --scenario 回放时由注入文件的 at_ms/advance_time 驱动（确定性）；
 * 否则真实节拍 */
static const sim_opts_t *g_opts; /* main 初始化后只读 */
static int64_t g_scenario_clock_ms = 0; /* --scenario 虚拟时钟（回放器推进） */
static int g_scenario_mode = 0;
static uint32_t sim_now_ms(void)
{
    if (g_scenario_mode) return (uint32_t)g_scenario_clock_ms;
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
    rt->selected_page = o->page; /* --page；强制页由 Presenter 按电源态仲裁 */
}

/*---------- --battery-seq：电池样本经真实 Power FSM（P5.1 共享实现）----------
 * 契约：INTERFACES §4「battery_sample 经与固件相同的 Power FSM 产生
 * LOW BATTERY，不能只换一张图冒充保护已验证」。本函数不提供绕过 FSM 的路径：
 * runtime.power_state 只取 cdt_power_step 的终态。 */

static int run_battery_fsm(const char *seq, cdt_runtime_t *rt)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t params;
    cdt_power_input_t in;
    cdt_power_action_t actions;
    long mv, ms;
    char buf[1024];
    char *p, *tok;
    bool have_last = false;
    long last_mv = 0;

    if (seq == NULL || seq[0] == '\0') {
        fprintf(stderr, "[sim] ERROR: --battery-seq empty\n");
        return -1;
    }
    if (strlen(seq) >= sizeof(buf)) {
        fprintf(stderr, "[sim] ERROR: --battery-seq too long (max %zu chars)\n",
                sizeof(buf) - 1);
        return -1;
    }
    snprintf(buf, sizeof(buf), "%s", seq);

    cdt_power_params_init(&params);
    cdt_power_init(&fsm, &params, false);

    tok = buf;
    while (tok != NULL && *tok != '\0') {
        p = strchr(tok, ',');
        if (p != NULL) *p = '\0';
        if (sscanf(tok, "%ld@%ld", &mv, &ms) != 2 || ms < 0 ||
            mv < 0 || mv > 65535) {
            fprintf(stderr, "[sim] ERROR: --battery-seq token '%s' (want mv@ms)\n", tok);
            return -1;
        }
        in.kind = CDT_POWER_IN_SAMPLE;
        in.sample.battery_mv = (uint16_t)mv;
        in.sample.valid = (mv >= params.valid_min_mv && mv <= params.valid_max_mv);
        in.sample.at_ms = (int64_t)ms;
        actions = cdt_power_step(&fsm, &in, (int64_t)ms);
        if (actions != CDT_POWER_ACT_NONE) {
            char abuf[160];
            cdt_power_actions_str(abuf, sizeof(abuf), actions);
            printf("[sim] FSM t=%ldms %s -> %s (%s)\n", ms, tok,
                   cdt_power_state_str(fsm.state), abuf);
        }
        last_mv = mv;
        have_last = true;
        tok = (p != NULL) ? p + 1 : NULL;
    }

    if (!have_last) {
        fprintf(stderr, "[sim] ERROR: --battery-seq has no samples\n");
        return -1;
    }
    /* runtime = FSM 终态 + 最后一个样本值（保护态只能来自 FSM） */
    rt->power_state = fsm.state;
    rt->battery_valid = true;
    rt->battery_mv = (uint16_t)last_mv;
    rt->usable_percent = usable_percent_from_mv(last_mv);
    printf("[sim] FSM final state: %s, battery=%ldmV usable=%u%%\n",
           cdt_power_state_str(fsm.state), last_mv,
           (unsigned)rt->usable_percent);
    fflush(stdout);
    return 0;
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

/*---------- SDL 事件观察（不消费事件，LVGL 的 SDL 驱动仍照常轮询） ----------
 * KEY 不在事件回调里直接操作 LVGL（避免与 lv_timer_handler 重入）：
 * 只记录 pending，主循环每拍应用一次。 */

static const cdt_app_state_t *g_state; /* main 初始化后只读（可 NULL） */
static cdt_runtime_t *g_runtime;       /* main 的 runtime（导航写回目标） */
static cdt_nav_t g_nav;                /* 导航状态（宿主持有） */
static cdt_view_t g_last_view;         /* 最近一次 present 结果 */
static volatile int g_pending_key = 0; /* 0=无；否则 cdt_key_event_t */

static void sim_key_received(cdt_key_event_t ev)
{
    printf("[sim] KEY event: %s\n", ev == CDT_KEY_LONG_PRESS ? "long_press" : "short_press");
    fflush(stdout);
    g_pending_key = (int)ev;
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
                case SDLK_RIGHT:
                    sim_key_received(CDT_KEY_SHORT_PRESS);
                    break;
                case SDLK_m:
                    sim_key_received(CDT_KEY_LONG_PRESS);
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

/*---------- KEY → 导航/静音 → DeviceRuntime 写回（P2.2 宿主接线） ----------*/

static void sim_apply_pending_key(lv_display_t *disp)
{
    cdt_key_event_t ev = (cdt_key_event_t)g_pending_key;
    uint32_t act;
    cdt_view_t view;

    g_pending_key = 0;

    act = cdt_nav_key(&g_nav, &g_last_view, ev);
    if (act & CDT_NAV_ACT_PAGE) {
        g_runtime->selected_page = g_nav.page; /* 主页面真源在 DeviceRuntime */
        printf("[sim] nav -> page %d (sub agents=%u plan=%u)\n", (int)g_nav.page,
               (unsigned)g_nav.agents_page, (unsigned)g_nav.plan_page);
    }
    if (act & CDT_NAV_ACT_MUTE) {
        /* 长按只静音当前提醒（ACK=本地静音/已读，绝不等于批准操作）；
         * 静音标识=当前选中线程 id（attention 归属线程）。 */
        g_runtime->muted_attention_present = true;
        if (g_state != NULL && g_state->thread_count > 0) {
            const char *id = (g_state->selected_thread_id_present)
                                 ? g_state->selected_thread_id
                                 : g_state->threads[0].id;
            snprintf(g_runtime->muted_attention_id, sizeof(g_runtime->muted_attention_id),
                     "%s", id);
        }
        printf("[sim] muted current reminder (page unchanged, forced page NOT dismissed)\n");
    }
    if (act == CDT_NAV_ACT_NONE && ev == CDT_KEY_SHORT_PRESS) {
        printf("[sim] short press REJECTED (forced LOW BATTERY page or subpage end)\n");
    }

    /* 立即重渲染（确定性模式固定时钟下内容仍确定） */
    cdt_present(g_state, g_runtime, sim_now_ms(), &view);
    g_last_view = view;
    (void)cdt_nav_clamp(&g_nav, &view);
    cdt_ui_apply_nav(&view, &g_nav);
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(disp);
    fflush(stdout);
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

/*========== --scenario：注入 JSONL 回放（P2.4，契约 tests/UI_CONTRACT.md §3）==========
 * 先全量预检（未知 action、at_ms/advance_time 回退、payload 不符、AppState 非法
 * 都在出帧前失败，退出码 1，打印行号+原因），再逐条 apply。每条 action 后
 * present+整屏重渲染，逻辑帧与上一帧逐字节比较，有变化才导出 PNG 并记 manifest。
 * 确定性：时钟只来自 at_ms/advance_time；不读墙钟；无动画。 */

#define SC_MAX_ACTIONS 512
#define SC_LINE_MAX (CDT_STATE_JSON_MAX_BYTES + 256)

typedef enum {
    SC_ACT_APP_STATE = 1,
    SC_ACT_BATTERY,
    SC_ACT_KEY,
    SC_ACT_LINK,
    SC_ACT_ADVANCE
} sc_action_kind_t;

typedef struct {
    long line_no;          /* 文件行号（报错用） */
    long at_ms;            /* 虚拟时钟调度点 */
    sc_action_kind_t kind;
    char tag[48];          /* 可选 checkpoint 标签（空串=无） */
    const char *state_json;/* APP_STATE：payload 原始字节（指向行缓冲） */
    size_t state_len;
    uint16_t battery_mv;   /* BATTERY */
    bool battery_valid;
    int key_long;          /* KEY：0=short 1=long */
    int link_state;        /* LINK：cdt_link_state_t 值 */
    long advance_to_ms;    /* ADVANCE */
} sc_action_t;

static sc_action_t g_sc_actions[SC_MAX_ACTIONS];
static int g_sc_count = 0;
static char *g_sc_buf = NULL; /* 整文件行缓冲（action 的指针指向其中） */

/* 场景行解析错误：统一打印行号+原因后以退出码 1 结束（契约 §3.5） */
static void sc_fail(long line_no, const char *why)
{
    fprintf(stderr, "[sim] scenario ERROR (line %ld): %s\n", line_no, why);
    exit(1);
}

static int sc_kind_of(const char *s)
{
    if (strcmp(s, "app_state") == 0) return SC_ACT_APP_STATE;
    if (strcmp(s, "battery_sample") == 0) return SC_ACT_BATTERY;
    if (strcmp(s, "key") == 0) return SC_ACT_KEY;
    if (strcmp(s, "link") == 0) return SC_ACT_LINK;
    if (strcmp(s, "advance_time") == 0) return SC_ACT_ADVANCE;
    return 0;
}

static long sc_parse_ms(const cdt_app_state_t *unused, int64_t v, long line_no)
{
    (void)unused;
    if (v < 0 || v > 0x7FFFFFFFL) sc_fail(line_no, "at_ms/to_ms 越界（0..2^31-1）");
    return (long)v;
}

/* 解析并全量预检场景文件；违规 sc_fail（退出码 1），成功返回条数 */
static int sc_load(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        fprintf(stderr, "[sim] ERROR: cannot open scenario file '%s'\n", path);
        exit(2);
    }
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); exit(2); }
    long n = ftell(f);
    if (n < 0 || n > 4 * 1024 * 1024) {
        fprintf(stderr, "[sim] ERROR: scenario file '%s' too large (%ld bytes)\n", path, n);
        fclose(f);
        exit(2);
    }
    fseek(f, 0, SEEK_SET);
    g_sc_buf = (char *)malloc((size_t)n + 1);
    if (g_sc_buf == NULL || fread(g_sc_buf, 1, (size_t)n, f) != (size_t)n) {
        fprintf(stderr, "[sim] ERROR: cannot read scenario file '%s'\n", path);
        fclose(f);
        exit(2);
    }
    g_sc_buf[n] = '\0';
    fclose(f);

    long prev_at = -1;
    char *line = g_sc_buf;
    long line_no = 0;
    while (line != NULL && *line != '\0') {
        char *nl = strchr(line, '\n');
        if (nl != NULL) *nl = '\0';
        line_no++;

        /* 去行尾 \r；跳过空行与 # 注释 */
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == ' '
                           || line[len - 1] == '\t')) {
            line[--len] = '\0';
        }
        const char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (*s == '\0' || *s == '#') {
            line = (nl != NULL) ? nl + 1 : NULL;
            continue;
        }

        if (g_sc_count >= SC_MAX_ACTIONS) {
            sc_fail(line_no, "action 数超上限（SC_MAX_ACTIONS=512）");
        }
        sc_action_t *a = &g_sc_actions[g_sc_count];
        memset(a, 0, sizeof(*a));
        a->line_no = line_no;

        /* 逐字段解析 {"at_ms","action","payload"[,"tag"]}；未知键拒绝（预检） */
        cdt_json_t j;
        cdtj_init(&j, s, strlen(s));
        if (cdtj_peek(&j) != '{') sc_fail(line_no, "行必须是 JSON 对象");
        cdtj_expect(&j, '{');
        bool have_at = false, have_action = false, have_payload = false;
        int64_t at_ms = 0;
        char key[24];
        for (;;) {
            if (cdtj_peek(&j) == '}') break;
            if (have_at || have_action || have_payload) cdtj_expect(&j, ',');
            size_t klen = 0;
            if (cdtj_read_string(&j, key, sizeof(key), &klen) != CDT_PARSE_OK) {
                sc_fail(line_no, "键名非法");
            }
            cdtj_expect(&j, ':');
            if (strcmp(key, "at_ms") == 0) {
                bool is_int = false;
                if (cdtj_read_number(&j, &is_int, &at_ms, NULL) != CDT_PARSE_OK || !is_int) {
                    sc_fail(line_no, "at_ms 必须是非负整数字面量");
                }
                have_at = true;
            }
            else if (strcmp(key, "action") == 0) {
                char act[24];
                size_t alen = 0;
                if (cdtj_read_string(&j, act, sizeof(act), &alen) != CDT_PARSE_OK) {
                    sc_fail(line_no, "action 非法");
                }
                a->kind = (sc_action_kind_t)sc_kind_of(act);
                if (a->kind == 0) sc_fail(line_no, "未知 action（契约 §3.2）");
                have_action = true;
            }
            else if (strcmp(key, "tag") == 0) {
                char tag[48];
                size_t tlen = 0;
                if (cdtj_read_string(&j, tag, sizeof(tag), &tlen) != CDT_PARSE_OK ||
                    tlen == 0) {
                    sc_fail(line_no, "tag 非法或为空");
                }
                memcpy(a->tag, tag, sizeof(a->tag));
            }
            else if (strcmp(key, "payload") == 0) {
                have_payload = true;
                if (cdtj_peek(&j) != '{') sc_fail(line_no, "payload 必须是对象");
                const uint8_t *pstart = j.p;
                if (cdtj_skip_value(&j) != CDT_PARSE_OK) {
                    sc_fail(line_no, "payload JSON 非法");
                }
                a->state_json = (const char *)pstart;
                a->state_len = (size_t)(j.p - pstart);
            }
            else {
                sc_fail(line_no, "未知键（只允许 at_ms/action/payload/tag）");
            }
        }
        cdtj_expect(&j, '}');
        if (cdtj_expect_eof(&j) != CDT_PARSE_OK) sc_fail(line_no, "对象后有多余内容");
        if (!have_at || !have_action || !have_payload) {
            sc_fail(line_no, "缺少 at_ms/action/payload 之一");
        }
        a->at_ms = sc_parse_ms(NULL, at_ms, line_no);
        if (a->at_ms < prev_at) sc_fail(line_no, "at_ms 回退（必须单调不减）");
        prev_at = a->at_ms;

        /* payload 形状预检（契约 §3.2）：app_state 走完整 shared/state 解析 */
        switch (a->kind) {
            case SC_ACT_APP_STATE: {
                cdt_app_state_t st;
                cdt_parse_result_t r = cdt_state_parse((const uint8_t *)a->state_json,
                                                       a->state_len, &st);
                if (r != CDT_PARSE_OK) {
                    char why[96];
                    snprintf(why, sizeof(why), "AppState 校验失败（cdt_state_parse code %d）",
                             (int)r);
                    sc_fail(line_no, why);
                }
                break;
            }
            case SC_ACT_BATTERY: {
                cdt_json_t p;
                cdtj_init(&p, a->state_json, a->state_len);
                cdtj_expect(&p, '{');
                bool have_mv = false, have_valid = false, mv_null = false;
                int64_t mv = 0;
                bool valid = false;
                char key2[24];
                while (cdtj_peek(&p) != '}') {
                    size_t klen = 0;
                    if (cdtj_read_string(&p, key2, sizeof(key2), &klen) != CDT_PARSE_OK) {
                        sc_fail(line_no, "battery payload 键名非法");
                    }
                    cdtj_expect(&p, ':');
                    if (strcmp(key2, "battery_mv") == 0) {
                        bool is_int = false;
                        if (cdtj_peek(&p) == 'n') { /* null（invalid 采样） */
                            if (cdtj_expect_null(&p) != CDT_PARSE_OK) {
                                sc_fail(line_no, "battery_mv 字面量非法");
                            }
                            mv_null = true;
                        }
                        else if (cdtj_read_number(&p, &is_int, &mv, NULL) != CDT_PARSE_OK ||
                                 !is_int || mv < 0 || mv > 65535) {
                            sc_fail(line_no, "battery_mv 必须 0..65535 整数或 null");
                        }
                        have_mv = true;
                    }
                    else if (strcmp(key2, "battery_valid") == 0) {
                        if (cdtj_read_bool(&p, &valid) != CDT_PARSE_OK) {
                            sc_fail(line_no, "battery_valid 必须是 bool");
                        }
                        have_valid = true;
                    }
                    else {
                        sc_fail(line_no, "未知 battery payload 键");
                    }
                    if (cdtj_peek(&p) == ',') cdtj_expect(&p, ',');
                }
                cdtj_expect(&p, '}');
                if (!have_mv || !have_valid) sc_fail(line_no, "battery payload 缺字段");
                if (!valid && !mv_null) {
                    sc_fail(line_no, "battery_valid=false 时 battery_mv 必须 null");
                }
                if (valid && mv_null) {
                    sc_fail(line_no, "battery_valid=true 时 battery_mv 不能为 null");
                }
                a->battery_mv = (uint16_t)mv;
                a->battery_valid = valid;
                break;
            }
            case SC_ACT_KEY: {
                cdt_json_t p;
                cdtj_init(&p, a->state_json, a->state_len);
                cdtj_expect(&p, '{');
                char key2[24], val[24];
                size_t klen = 0, vlen = 0;
                if (cdtj_read_string(&p, key2, sizeof(key2), &klen) != CDT_PARSE_OK ||
                    strcmp(key2, "key") != 0) {
                    sc_fail(line_no, "key payload 需 {\"key\": ...}");
                }
                cdtj_expect(&p, ':');
                if (cdtj_read_string(&p, val, sizeof(val), &vlen) != CDT_PARSE_OK) {
                    sc_fail(line_no, "key 值非法");
                }
                if (strcmp(val, "short_press") == 0) a->key_long = 0;
                else if (strcmp(val, "long_press") == 0) a->key_long = 1;
                else sc_fail(line_no, "key 值须 short_press|long_press");
                cdtj_expect(&p, '}');
                break;
            }
            case SC_ACT_LINK: {
                cdt_json_t p;
                cdtj_init(&p, a->state_json, a->state_len);
                cdtj_expect(&p, '{');
                char key2[24], val[24];
                size_t klen = 0, vlen = 0;
                if (cdtj_read_string(&p, key2, sizeof(key2), &klen) != CDT_PARSE_OK ||
                    strcmp(key2, "link_state") != 0) {
                    sc_fail(line_no, "link payload 需 {\"link_state\": ...}");
                }
                cdtj_expect(&p, ':');
                if (cdtj_read_string(&p, val, sizeof(val), &vlen) != CDT_PARSE_OK) {
                    sc_fail(line_no, "link_state 值非法");
                }
                if (strcmp(val, "connected") == 0) a->link_state = CDT_LINK_CONNECTED;
                else if (strcmp(val, "stale") == 0) a->link_state = CDT_LINK_STALE;
                else if (strcmp(val, "disconnected") == 0) {
                    a->link_state = CDT_LINK_DISCONNECTED;
                }
                else sc_fail(line_no, "link_state 值非法");
                cdtj_expect(&p, '}');
                break;
            }
            case SC_ACT_ADVANCE: {
                cdt_json_t p;
                cdtj_init(&p, a->state_json, a->state_len);
                cdtj_expect(&p, '{');
                char key2[24];
                size_t klen = 0;
                int64_t to_ms = 0;
                bool is_int = false;
                if (cdtj_read_string(&p, key2, sizeof(key2), &klen) != CDT_PARSE_OK ||
                    strcmp(key2, "to_ms") != 0) {
                    sc_fail(line_no, "advance_time payload 需 {\"to_ms\": N}");
                }
                cdtj_expect(&p, ':');
                if (cdtj_read_number(&p, &is_int, &to_ms, NULL) != CDT_PARSE_OK || !is_int) {
                    sc_fail(line_no, "to_ms 必须是非负整数");
                }
                a->advance_to_ms = sc_parse_ms(NULL, to_ms, line_no);
                cdtj_expect(&p, '}');
                if (a->advance_to_ms < a->at_ms) {
                    sc_fail(line_no, "advance_time to_ms < at_ms");
                }
                break;
            }
            default:
                sc_fail(line_no, "内部：未知 action kind");
        }
        g_sc_count++;
        line = (nl != NULL) ? nl + 1 : NULL;
    }
    if (g_sc_count == 0) sc_fail(1, "场景文件没有任何 action");
    return g_sc_count;
}

/* ---- PNG 输出：400x300 灰度 8bit、无滤波、zlib stored 块（字节确定） ---- */

static uint32_t sc_crc32(const uint8_t *d, size_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    size_t i;
    int k;
    for (i = 0; i < n; i++) {
        c ^= d[i];
        for (k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
        }
    }
    return c ^ 0xFFFFFFFFu;
}

static uint32_t sc_adler32(const uint8_t *d, size_t n)
{
    uint32_t a = 1, b = 0;
    size_t i;
    for (i = 0; i < n; i++) {
        a = (a + d[i]) % 65521u;
        b = (b + a) % 65521u;
    }
    return (b << 16) | a;
}

static void sc_png_chunk(FILE *fp, const char *type, const uint8_t *body, uint32_t len)
{
    uint8_t hdr[8];
    uint8_t crcb[4];
    uint8_t *tmp;
    uint32_t crc;
    hdr[0] = (uint8_t)(len >> 24);
    hdr[1] = (uint8_t)(len >> 16);
    hdr[2] = (uint8_t)(len >> 8);
    hdr[3] = (uint8_t)len;
    hdr[4] = (uint8_t)type[0];
    hdr[5] = (uint8_t)type[1];
    hdr[6] = (uint8_t)type[2];
    hdr[7] = (uint8_t)type[3];
    fwrite(hdr, 1, 8, fp);
    if (len > 0) fwrite(body, 1, len, fp);
    tmp = (uint8_t *)malloc((size_t)len + 4);
    if (tmp != NULL) {
        memcpy(tmp, hdr + 4, 4);
        if (len > 0) memcpy(tmp + 4, body, len);
        crc = sc_crc32(tmp, (size_t)len + 4);
        free(tmp);
    }
    else {
        crc = 0; /* malloc 失败时 CRC 错误 → 文件损坏可被检查发现，不静默 */
    }
    crcb[0] = (uint8_t)(crc >> 24);
    crcb[1] = (uint8_t)(crc >> 16);
    crcb[2] = (uint8_t)(crc >> 8);
    crcb[3] = (uint8_t)crc;
    fwrite(crcb, 1, 4, fp);
}

static int write_frame_png(const cdt_frame_t *f, const char *path)
{
    /* 原始扫描线：每行 1 字节滤波 0 + 400 字节灰度（黑=0/白=255） */
    static uint8_t raw[(1 + SIM_HOR_RES) * SIM_VER_RES];
    static uint8_t zbuf[sizeof(raw) + 64];
    size_t raw_len = 0, z_len = 0;
    int x, y;
    FILE *fp;

    for (y = 0; y < SIM_VER_RES; y++) {
        raw[raw_len++] = 0; /* filter: None */
        for (x = 0; x < SIM_HOR_RES; x++) {
            int bit = cdt_frame_get(f, x, y); /* 1=黑 */
            raw[raw_len++] = bit ? 0x00 : 0xFF;
        }
    }

    /* zlib 流：0x78 0x01 + stored deflate 块（每块 ≤65535）+ adler32 */
    zbuf[z_len++] = 0x78;
    zbuf[z_len++] = 0x01;
    {
        size_t off = 0;
        while (off < raw_len) {
            size_t blk = raw_len - off;
            if (blk > 65535) blk = 65535;
            int last = (off + blk >= raw_len);
            zbuf[z_len++] = last ? 1 : 0;
            zbuf[z_len++] = (uint8_t)(blk & 0xFF);
            zbuf[z_len++] = (uint8_t)(blk >> 8);
            zbuf[z_len++] = (uint8_t)(~blk & 0xFF);
            zbuf[z_len++] = (uint8_t)((~blk >> 8) & 0xFF);
            memcpy(zbuf + z_len, raw + off, blk);
            z_len += blk;
            off += blk;
        }
    }
    {
        uint32_t ad = sc_adler32(raw, raw_len);
        zbuf[z_len++] = (uint8_t)(ad >> 24);
        zbuf[z_len++] = (uint8_t)(ad >> 16);
        zbuf[z_len++] = (uint8_t)(ad >> 8);
        zbuf[z_len++] = (uint8_t)ad;
    }

    fp = fopen(path, "wb");
    if (fp == NULL) return -1;
    {
        uint8_t ihdr[13];
        uint32_t w = SIM_HOR_RES, h = SIM_VER_RES;
        ihdr[0] = (uint8_t)(w >> 24);
        ihdr[1] = (uint8_t)(w >> 16);
        ihdr[2] = (uint8_t)(w >> 8);
        ihdr[3] = (uint8_t)w;
        ihdr[4] = (uint8_t)(h >> 24);
        ihdr[5] = (uint8_t)(h >> 16);
        ihdr[6] = (uint8_t)(h >> 8);
        ihdr[7] = (uint8_t)h;
        ihdr[8] = 8;  /* bit depth */
        ihdr[9] = 0;  /* color type: grayscale */
        ihdr[10] = 0; /* compression */
        ihdr[11] = 0; /* filter */
        ihdr[12] = 0; /* interlace */
        fwrite("\x89PNG\r\n\x1a\n", 1, 8, fp);
        sc_png_chunk(fp, "IHDR", ihdr, 13);
        sc_png_chunk(fp, "IDAT", zbuf, (uint32_t)z_len);
        sc_png_chunk(fp, "IEND", NULL, 0);
    }
    fclose(fp);
    return 0;
}

/* ---- 回放执行 ---- */

static const char *sc_page_name(const cdt_view_t *v)
{
    switch (v->page) {
        case CDT_PAGE_AGENTS: return "agents";
        case CDT_PAGE_PLAN: return "plan";
        case CDT_PAGE_USAGE: return "usage";
        case CDT_PAGE_LOW_BATTERY: return "low_battery";
        case CDT_PAGE_DETAILS: return "details";
        default: return "now";
    }
}

static void sc_json_str(FILE *fp, const char *s)
{
    fputc('"', fp);
    for (; s != NULL && *s != '\0'; s++) {
        if (*s == '"' || *s == '\\') fputc('\\', fp);
        fputc(*s, fp);
    }
    fputc('"', fp);
}

/* manifest 一行（契约 §3.4 + 扩展 view 字段供 check_ui 语义断言）：
 * {"frame": <名|null>, "scenario": …, "frame_index": <n|null>, "at_ms": …,
 *  "action": …, "seq": <seq|null>, "tag": <可选>, "view": {…}} */
static void sc_manifest_line(FILE *mf, const char *scen,
                             const char *frame_name, int frame_index,
                             long at_ms, const char *action, const char *tag,
                             const cdt_app_state_t *st, const cdt_view_t *v)
{
    fprintf(mf, "{\"frame\": ");
    if (frame_name != NULL) sc_json_str(mf, frame_name);
    else fprintf(mf, "null");
    fprintf(mf, ", \"scenario\": \"%s\", \"frame_index\": ", scen);
    if (frame_index > 0) fprintf(mf, "%d", frame_index);
    else fprintf(mf, "null");
    fprintf(mf, ", \"at_ms\": %ld, \"action\": \"%s\", \"seq\": ", at_ms, action);
    if (st != NULL) fprintf(mf, "%llu", (unsigned long long)st->seq);
    else fprintf(mf, "null");
    if (tag != NULL && tag[0] != '\0') {
        fprintf(mf, ", \"tag\": ");
        sc_json_str(mf, tag);
    }
    fprintf(mf, ", \"view\": {\"page\": \"%s\", \"status\": ", sc_page_name(v));
    sc_json_str(mf, v->status_label);
    fprintf(mf, ", \"project\": ");
    sc_json_str(mf, v->project);
    fprintf(mf, ", \"activity\": ");
    sc_json_str(mf, v->activity);
    fprintf(mf, ", \"elapsed\": ");
    sc_json_str(mf, v->elapsed_text);
    fprintf(mf, ", \"waiting\": ");
    sc_json_str(mf, v->waiting_text);
    fprintf(mf, ", \"frozen\": %s, \"cancelled\": %s, \"forced\": %s, "
                "\"link_stale\": %s, \"link_disconnected\": %s",
            v->time_frozen ? "true" : "false",
            v->cancelled ? "true" : "false",
            v->low_battery_forced ? "true" : "false",
            v->link_stale ? "true" : "false",
            v->link_disconnected ? "true" : "false");
    fprintf(mf, ", \"attention_present\": %s, \"pending\": %u, \"muted\": %s",
            v->attention_present ? "true" : "false",
            (unsigned)v->pending_count,
            v->muted ? "true" : "false");
    fprintf(mf, ", \"plan\": ");
    if (v->plan_present) sc_json_str(mf, v->plan_text);
    else fprintf(mf, "null");
    fprintf(mf, ", \"usage\": ");
    sc_json_str(mf, v->usage_text);
    fprintf(mf, ", \"voltage\": ");
    sc_json_str(mf, v->voltage_text);
    fprintf(mf, ", \"ctx\": ");
    sc_json_str(mf, v->context_text);
    fprintf(mf, ", \"agents_count\": %u, \"agents_pages\": %u, \"agents_hidden\": %u, "
                "\"plan_step_count\": %u, \"plan_total\": %u, \"plan_completed\": %u, "
                "\"plan_pages\": %u, \"usage_count\": %u",
            (unsigned)v->agents_count, (unsigned)v->agents_pages,
            (unsigned)v->agents_hidden,
            (unsigned)v->plan_step_count, (unsigned)v->plan_total,
            (unsigned)v->plan_completed, (unsigned)v->plan_pages,
            (unsigned)v->usage_count);
    /* 结构化行数据（check_ui 语义断言：AGENTS 排序 / PLAN 计数 / USAGE 行） */
    fprintf(mf, ", \"agents_states\": [");
    for (uint8_t i = 0; i < v->agents_count; i++) {
        if (i > 0) fputc(',', mf);
        sc_json_str(mf, v->agents_rows[i].state_label);
    }
    fprintf(mf, "], \"plan_statuses\": [");
    for (uint8_t i = 0; i < v->plan_step_count; i++) {
        if (i > 0) fputc(',', mf);
        const char *ps = "pending";
        if (v->plan_steps[i].status == (uint8_t)CDT_STEP_STATUS_COMPLETED) ps = "completed";
        else if (v->plan_steps[i].status == (uint8_t)CDT_STEP_STATUS_IN_PROGRESS) {
            ps = "in_progress";
        }
        sc_json_str(mf, ps);
    }
    fprintf(mf, "], \"usage_rows\": [");
    for (uint8_t i = 0; i < v->usage_count; i++) {
        const cdt_usage_row_t *r = &v->usage_rows[i];
        if (i > 0) fputc(',', mf);
        fprintf(mf, "{\"label\": ");
        sc_json_str(mf, r->label);
        fprintf(mf, ", \"pct_present\": %s, \"pct\": %u, \"mins\": %u, "
                    "\"reset_present\": %s, \"reset_in_s\": %d}",
                r->pct_present ? "true" : "false", (unsigned)r->pct,
                (unsigned)r->duration_mins,
                r->reset_present ? "true" : "false", (int)r->reset_in_s);
    }
    fprintf(mf, "]}");
    fputc('}', mf);
    fputc('\n', mf);
}

/* 回放执行：返回退出码（0=完成；1=违规——已在加载期处理） */
static void sc_file_stem(const char *path, char *out, size_t cap)
{
    const char *base = strrchr(path, '/');
    base = (base != NULL) ? base + 1 : path;
    const char *dot = strrchr(base, '.');
    size_t n = (dot != NULL) ? (size_t)(dot - base) : strlen(base);
    if (n >= cap) n = cap - 1;
    memcpy(out, base, n);
    out[n] = '\0';
}

static int sc_link_severity(int st)
{
    return (int)st; /* CONNECTED=1 < STALE=2 < DISCONNECTED=3 */
}

static void sc_run(lv_display_t *disp, const sim_opts_t *o,
                   cdt_app_state_t *state, cdt_runtime_t *runtime,
                   cdt_nav_t *nav)
{
    static cdt_frame_t cur, prev;
    static const char *ACT_NAME[6] = { NULL, "app_state", "battery_sample",
                                       "key", "link", "advance_time" };
    char stem[128], path[768], frame_name[160];
    FILE *mf;
    cdt_power_fsm_t fsm;
    cdt_power_params_t params;
    cdt_power_input_t in;
    cdt_power_action_t actions;
    int link_explicit = CDT_LINK_CONNECTED;
    int have_prev = 0, frame_no = 0, i;

    sc_file_stem(o->scenario, stem, sizeof(stem));
    snprintf(path, sizeof(path), "%s/%s", o->capture_dir, stem);
    {
        char mk[512];
        mkdir(o->capture_dir, 0755); /* 已存在忽略 */
        snprintf(mk, sizeof(mk), "%s", path);
        mkdir(mk, 0755);
    }

    /* 场景起点：无快照、电池 unknown（未注入 battery_sample 的场景电压显示
     * "--"，SCENARIOS §2）；last_rx=0，自然计时从虚拟时钟 0 起算 */
    memset(state, 0, sizeof(*state));
    g_state = NULL;
    runtime->battery_valid = false;
    runtime->battery_mv = 0;
    runtime->usable_percent = 0;
    runtime->last_rx_monotonic_ms = 0;

    snprintf(path, sizeof(path), "%s/%s/manifest.jsonl", o->capture_dir, stem);
    mf = fopen(path, "w");
    if (mf == NULL) {
        fprintf(stderr, "[sim] ERROR: cannot write %s\n", path);
        exit(2);
    }

    cdt_power_params_init(&params);
    cdt_power_init(&fsm, &params, false);

    for (i = 0; i < g_sc_count; i++) {
        const sc_action_t *a = &g_sc_actions[i];
        cdt_view_t view;
        const char *tag;

        if (a->kind == SC_ACT_ADVANCE) {
            g_scenario_clock_ms = a->advance_to_ms;
        }
        else if (a->at_ms > g_scenario_clock_ms) {
            g_scenario_clock_ms = a->at_ms;
        }

        switch (a->kind) {
            case SC_ACT_APP_STATE: {
                cdt_parse_result_t r = cdt_state_parse(
                    (const uint8_t *)a->state_json, a->state_len, state);
                if (r != CDT_PARSE_OK) {
                    fprintf(stderr, "[sim] scenario ERROR (line %ld): AppState "
                            "解析失败 code %d\n", a->line_no, (int)r);
                    exit(1);
                }
                g_state = state;
                /* 在线设备只在收到合法新 seq 快照时刷新 last_rx（INTERFACES §4） */
                runtime->last_rx_monotonic_ms = (uint32_t)a->at_ms;
                break;
            }
            case SC_ACT_BATTERY: {
                bool in_range = (a->battery_mv >= params.valid_min_mv &&
                                 a->battery_mv <= params.valid_max_mv);
                bool disp_valid = (a->battery_valid && in_range);
                in.kind = CDT_POWER_IN_SAMPLE;
                in.sample.battery_mv = a->battery_mv;
                in.sample.valid = disp_valid;
                in.sample.at_ms = (int64_t)a->at_ms;
                actions = cdt_power_step(&fsm, &in, (int64_t)a->at_ms);
                if (actions != CDT_POWER_ACT_NONE) {
                    char abuf[160];
                    cdt_power_actions_str(abuf, sizeof(abuf), actions);
                    printf("[sim] FSM t=%ldms %umV -> %s (%s)\n", a->at_ms,
                           (unsigned)a->battery_mv, cdt_power_state_str(fsm.state), abuf);
                }
                runtime->power_state = fsm.state; /* 保护态只来自 FSM（无捷径） */
                runtime->battery_valid = disp_valid;
                runtime->battery_mv = a->battery_mv;
                runtime->usable_percent = disp_valid
                    ? usable_percent_from_mv((long)a->battery_mv) : 0;
                break;
            }
            case SC_ACT_KEY:
                g_pending_key = a->key_long ? (int)CDT_KEY_LONG_PRESS
                                            : (int)CDT_KEY_SHORT_PRESS;
                break;
            case SC_ACT_LINK:
                link_explicit = a->link_state;
                break;
            default:
                break;
        }

        /* 自然计时（契约 §3.2：45s→stale、150s→disconnected；link 只提前注入。
         * 生效值取 显式注入 与 自然老化 中更严重者） */
        {
            int natural = CDT_LINK_CONNECTED;
            uint32_t age = (uint32_t)g_scenario_clock_ms - runtime->last_rx_monotonic_ms;
            if (age >= 150000) natural = CDT_LINK_DISCONNECTED;
            else if (age >= 45000) natural = CDT_LINK_STALE;
            runtime->link_state = (sc_link_severity(natural) > sc_link_severity(link_explicit))
                                      ? natural : link_explicit;
        }

        /* KEY 由宿主路径应用（导航/静音写回 + 立即重渲染） */
        if (g_pending_key != 0) {
            sim_apply_pending_key(disp);
        }

        /* present + 整页刷新（确定性：时钟固定、无动画） */
        cdt_present(g_state, runtime, sim_now_ms(), &view);
        g_last_view = view;
        (void)cdt_nav_clamp(nav, &view);
        cdt_ui_apply_nav(&view, nav);
        lv_obj_invalidate(lv_screen_active());
        lv_refr_now(disp);

        if (capture_logical_frame(disp, &cur) != 0) {
            fprintf(stderr, "[sim] scenario ERROR: capture failed at action %d\n", i + 1);
            exit(2);
        }

        tag = (a->tag[0] != '\0') ? a->tag : ACT_NAME[a->kind];
        if (!have_prev || memcmp(cur.px, prev.px, CDT_FRAME_BYTES) != 0) {
            frame_no++;
            snprintf(frame_name, sizeof(frame_name), "%s__f%03d_%s.png", stem,
                     frame_no, tag);
            snprintf(path, sizeof(path), "%s/%s/%s", o->capture_dir, stem,
                     frame_name);
            if (write_frame_png(&cur, path) != 0) {
                fprintf(stderr, "[sim] ERROR: cannot write %s\n", path);
                exit(2);
            }
            memcpy(prev.px, cur.px, CDT_FRAME_BYTES);
            have_prev = 1;
            sc_manifest_line(mf, stem, frame_name, frame_no, a->at_ms,
                             ACT_NAME[a->kind], a->tag, g_state, &view);
        }
        else {
            sc_manifest_line(mf, stem, NULL, 0, a->at_ms,
                             ACT_NAME[a->kind], a->tag, g_state, &view);
        }
    }
    fclose(mf);
    printf("[sim] scenario '%s': %d actions, %d frames -> %s/%s/\n", stem,
           g_sc_count, frame_no, o->capture_dir, stem);
    fflush(stdout);
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

    /* --scenario：加载期全量预检（违规 exit 1，不出帧；契约 §3.2） */
    if (opts.scenario != NULL) {
        g_scenario_mode = 1;
        g_scenario_clock_ms = 0;
        sc_load(opts.scenario);
        printf("[sim] scenario loaded: %d actions from %s\n", g_sc_count,
               opts.scenario);
        fflush(stdout);
    }

    /* --state 文件先于 SDL/LVGL 初始化解析：坏文件直接非 0 退出 */
    if (opts.state_path != NULL) {
        if (load_state_file(opts.state_path, &app_state) != 0) {
            return 2;
        }
        state_ptr = &app_state;
    }
    g_opts = &opts;
    synth_runtime(&opts, &runtime);
    if (opts.battery_seq != NULL) {
        /* 电池样本经真实 Power FSM（P5.1）产生保护态；无捷径直画低压页 */
        if (run_battery_fsm(opts.battery_seq, &runtime) != 0) {
            return 2;
        }
    }
    g_state = state_ptr;
    g_runtime = &runtime;
    cdt_nav_init(&g_nav, runtime.selected_page);

    printf("[sim] codex-display-sim (P2.2/P2.3) LVGL %d.%d.%d, %dx%d, LV_COLOR_DEPTH=%d\n",
           lv_version_major(), lv_version_minor(), lv_version_patch(),
           SIM_HOR_RES, SIM_VER_RES, LV_COLOR_DEPTH);
    if (state_ptr != NULL) {
        printf("[sim] state: %s (seq=%llu, threads=%u, epoch=%.16s) battery=%umV link=%s page=%d\n",
               opts.state_path, (unsigned long long)state_ptr->seq,
               (unsigned)state_ptr->thread_count, state_ptr->bridge_epoch,
               (unsigned)runtime.battery_mv,
               runtime.link_state == CDT_LINK_CONNECTED ? "connected" :
               runtime.link_state == CDT_LINK_STALE ? "stale" : "disconnected",
               (int)runtime.selected_page);
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
        g_last_view = view;
        cdt_ui_apply_nav(&view, &g_nav);
    }
    else if (opts.scenario != NULL) {
        cdt_app_state_t sc_state;

        memset(&sc_state, 0, sizeof(sc_state));
        cdt_ui_init();
        cdt_nav_init(&g_nav, runtime.selected_page);
        /* 基线（无快照占位渲染）不单独出帧；逐 action 应用后按变化出帧 */
        sc_run(disp, &opts, &sc_state, &runtime, &g_nav);
        sim_shutdown(disp, &opts, sdl_capture_env);
        return 0;
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
            g_last_view = view;
            cdt_ui_apply_nav(&view, &g_nav);
            lv_obj_invalidate(lv_screen_active());
        }

        if (g_pending_key != 0) {
            sim_apply_pending_key(disp);
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
