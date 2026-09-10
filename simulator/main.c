/**
 * @file main.c
 * codex-desk-terminal 模拟器骨架（P1.3 / A2）。
 *
 * 只做骨架：LVGL 初始化、400x300 SDL 窗口、事件循环、SDL 键盘→KEY 事件打印、
 * 占位画面（纯色背景 + 矩形 + 内置字体文本）、干净退出。不实现页面/State/Presenter。
 *
 * 键盘模拟（打印骨架，后续任务接入 ui_key/Runtime）：
 *   Space / Right  -> KEY 短按（打印）
 *   Esc            -> 退出
 *   窗口关闭按钮    -> LVGL LV_SDL_DIRECT_EXIT 路径（SDL_Quit + lv_deinit + exit(0)）
 *
 * 环境变量：
 *   SIM_AUTO_QUIT_MS   运行 N 毫秒后自动退出（离屏冒烟用；0/未设 = 常驻）
 *   SIM_CAPTURE_PATH   退出前将当前帧存为 BMP（最佳努力；Esc/自动退出路径有效，
 *                      窗口关闭按钮走 LVGL 直接退出路径，不会触发保存）
 */
#include <stdio.h>
#include <stdlib.h>

#include "lvgl.h"
#include LV_SDL_INCLUDE_PATH

#define SIM_HOR_RES 400
#define SIM_VER_RES 300

static volatile int g_quit_requested = 0;

/*---------- SDL 事件观察（不消费事件，LVGL 的 SDL 驱动仍照常轮询） ----------*/

static void key_event_skeleton(const char *sdl_name, const char *key_sem)
{
    /* KEY 事件骨架：P1.3 只打印；后续任务把事件交给 DeviceRuntime/ui_key()。
       这里就是“暂存点”——如需队列，在后续任务中替换此函数体。 */
    printf("[sim] KEY event: %s (SDL: %s)\n", key_sem, sdl_name);
    fflush(stdout);
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

/*---------- 退出前抓帧（BMP），用于渲染证据 ----------*/

static void sim_capture_frame(lv_display_t *disp, const char *path)
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
        fflush(stdout);
        SDL_FreeSurface(surf);
        return;
    }
    if(SDL_SaveBMP(surf, path) == 0) {
        printf("[sim] frame captured: %s (%dx%d)\n", path, w, h);
    }
    else {
        printf("[sim] capture: SaveBMP failed: %s\n", SDL_GetError());
    }
    fflush(stdout);
    SDL_FreeSurface(surf);
}

/*---------- 受控退出（Esc / 自动退出路径；窗口关闭由 LVGL 直接 exit(0)） ----------*/

static void sim_shutdown(lv_display_t *disp, const char *capture_path)
{
    if(capture_path) sim_capture_frame(disp, capture_path);

    lv_display_delete(disp);   /* 触发 release_disp_cb：销毁 texture/renderer/window，释放绘制缓冲 */
    lv_sdl_quit();             /* SDL_Quit() + 删除 LVGL 的 SDL 事件轮询定时器 */
    lv_deinit();               /* 释放 LVGL 全局状态与内存池 */
}

/*---------- 占位画面：纯色背景 + 矩形 + 内置字体文本 ----------*/

static void sim_build_placeholder_ui(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_t *rect;
    lv_obj_t *label;
    lv_obj_t *hint;

    /* 背景：白（I1 索引 1；SDL 驱动把 1 渲染为白、0 渲染为黑） */
    lv_obj_set_style_bg_color(scr, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(scr, 0, LV_PART_MAIN);

    /* 矩形：黑底白框小块（证明形状渲染） */
    rect = lv_obj_create(scr);
    lv_obj_remove_style_all(rect);
    lv_obj_set_size(rect, 96, 48);
    lv_obj_set_pos(rect, 16, 16);
    lv_obj_set_style_bg_color(rect, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(rect, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_color(rect, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_border_width(rect, 2, LV_PART_MAIN);
    lv_obj_set_style_radius(rect, 0, LV_PART_MAIN);

    /* 文本：内置 Montserrat 14（证明字体渲染） */
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

int main(void)
{
    const char *auto_quit_env = getenv("SIM_AUTO_QUIT_MS");
    const char *capture_path = getenv("SIM_CAPTURE_PATH");
    long auto_quit_ms = auto_quit_env ? atol(auto_quit_env) : 0;
    uint32_t t0, now, last_heartbeat;
    lv_display_t *disp;

    printf("[sim] codex-display-sim (P1.3 skeleton) LVGL %d.%d.%d, %dx%d, LV_COLOR_DEPTH=%d\n",
           lv_version_major(), lv_version_minor(), lv_version_patch(),
           SIM_HOR_RES, SIM_VER_RES, LV_COLOR_DEPTH);
    fflush(stdout);

    /* LVGL 初始化（lv_sdl_window_create 内部也会 SDL_Init，幂等） */
    lv_init();
    if(SDL_Init(SDL_INIT_VIDEO) != 0) {
        printf("[sim] SDL_Init failed: %s\n", SDL_GetError());
        return 1;
    }
    /* 事件观察者：不消费事件，只做应用级 KEY/退出骨架 */
    SDL_AddEventWatch(sim_event_watch, NULL);

    disp = lv_sdl_window_create(SIM_HOR_RES, SIM_VER_RES);
    if(disp == NULL) {
        printf("[sim] lv_sdl_window_create failed\n");
        SDL_Quit();
        return 1;
    }
    lv_sdl_window_set_title(disp, "codex-desk-terminal sim");
    lv_sdl_window_set_resizeable(disp, false);

    /* 打印窗口几何，供窗口截图定位（人工运行证据） */
    {
        SDL_Renderer *ren = (SDL_Renderer *)lv_sdl_window_get_renderer(disp);
        SDL_Window *win = ren ? SDL_RenderGetWindow(ren) : NULL;
        if(win) {
            int wx, wy, ww, wh;
            SDL_GetWindowPosition(win, &wx, &wy);
            SDL_GetWindowSize(win, &ww, &wh);
            printf("[sim] window geometry: x=%d y=%d w=%d h=%d\n", wx, wy, ww, wh);
            fflush(stdout);
        }
    }

    sim_build_placeholder_ui();
    /* 立即渲染一帧，保证无输入时屏幕内容也已出现 */
    lv_refr_now(disp);

    t0 = SDL_GetTicks();
    last_heartbeat = t0;
    printf("[sim] entering main loop%s\n", auto_quit_ms > 0 ? " (auto-quit)" : "");
    fflush(stdout);

    while(!g_quit_requested) {
        uint32_t next = lv_timer_handler();

        now = SDL_GetTicks();
        if(auto_quit_ms > 0 && (now - t0) >= (uint32_t)auto_quit_ms) {
            printf("[sim] auto-quit after %lums\n", (unsigned long)(now - t0));
            fflush(stdout);
            break;
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
    sim_shutdown(disp, capture_path);
    printf("[sim] clean exit\n");
    fflush(stdout);
    return 0;
}
