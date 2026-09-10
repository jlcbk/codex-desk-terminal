/**
 * @file lv_conf.h
 * codex-desk-terminal 模拟器（P1.3 / A2）的 LVGL 9.3.0 配置。
 *
 * - LV_COLOR_DEPTH = 1：400x300 单色是项目不可变约束（docs/DEVELOPMENT_PLAN.md §1）。
 *   LVGL 9.3 的 SDL 驱动支持 I1（flush 内部 I1→ARGB8888 转换），
 *   但要求 LV_SDL_RENDER_MODE = LV_DISPLAY_RENDER_MODE_PARTIAL（源码里有 #error 守卫）。
 * - 共享逻辑帧（400x300、1bpp、每行 50 字节、MSB=左像素、1=黑/0=白，
 *   docs/INTERFACES.md §5）属于后续 shared/display 任务，本文件只约束 LVGL 渲染深度。
 * - 未在此处定义的选项回落到 vendor/lvgl/src/lv_conf_internal.h 的默认值。
 */

#ifndef LV_CONF_H
#define LV_CONF_H

/*====================
   颜色 / 内存 / 时基
 *===================*/
#define LV_COLOR_DEPTH 1

#define LV_MEM_SIZE (128 * 1024U)          /* 内置分配器池：128KB（任务指定默认或 128KB） */
#define LV_MEM_ADR 0

#define LV_USE_OS LV_OS_NONE               /* 单线程主循环，无 OS 依赖 */

/*====================
   SDL 宿主（软件渲染）
 *===================*/
#define LV_USE_SDL 1
#define LV_SDL_INCLUDE_PATH <SDL2/SDL.h>
#define LV_SDL_RENDER_MODE LV_DISPLAY_RENDER_MODE_PARTIAL  /* 深度 1 下的唯一合法取值 */
#define LV_SDL_BUF_COUNT 1
#define LV_SDL_ACCELERATED 0               /* 强制软件渲染（项目要求） */
#define LV_SDL_FULLSCREEN 0
#define LV_SDL_DIRECT_EXIT 1               /* 关闭窗口：SDL_Quit + lv_deinit + exit(0) */
#define LV_SDL_MOUSEWHEEL_MODE LV_SDL_MOUSEWHEEL_MODE_ENCODER

#define LV_USE_DRAW_SDL 0                  /* 不用 SDL GPU 绘制单元，走 LVGL 软件绘制 */

/*====================
   精简功能面（体积/构建时间）
 *===================*/
#define LV_USE_LOG 0

#define LV_USE_VECTOR_GRAPHIC 0
#define LV_USE_FFMPEG 0
#define LV_USE_LOTTIE 0
#define LV_USE_SNAPSHOT 0
#define LV_USE_IMGFONT 0
#define LV_USE_GRIDNAV 0
#define LV_USE_SPAN 0

#define LV_USE_LIBPNG 0
#define LV_USE_LIBJPEG_TURBO 0
#define LV_USE_TJPGD 0
#define LV_USE_BMP 0
#define LV_USE_GIF 0
#define LV_USE_QRCODE 0
#define LV_USE_FREETYPE 0
#define LV_USE_TINY_TTF 0

/*====================
   主题 / 字体 / 布局
 *===================*/
#define LV_USE_THEME_DEFAULT 1
#define LV_USE_THEME_MONO 0
#define LV_USE_THEME_SIMPLE 0

#define LV_FONT_MONTSERRAT_14 1            /* 占位画面文本（内置字体） */
#define LV_FONT_MONTSERRAT_16 0
#define LV_FONT_MONTSERRAT_20 0

#define LV_USE_FLEX 1
#define LV_USE_GRID 0

#endif /*LV_CONF_H*/
