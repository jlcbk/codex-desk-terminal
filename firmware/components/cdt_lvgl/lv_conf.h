/**
 * @file lv_conf.h
 * codex-desk-terminal 固件（P4.3 / A2+A3）的 LVGL 9.3.0 配置。
 *
 * 真源：simulator/lv_conf.h（P1.3/P2 golden 流的配置）。帧哈希对齐要求两端
 * "同 LVGL 版本 + 同 lv_conf 渲染相关项"——本文件除宿主差异项（SDL→无、
 * tick→esp_timer）外逐宏与模拟器一致；渲染面（颜色深度/字体/主题/布局/
 * 默认对齐与绘制选项）不得单端改动。
 *
 * - LV_COLOR_DEPTH = 1：400x300 单色是项目不可变约束（docs/DEVELOPMENT_PLAN.md §1）。
 *   LVGL I1 位语义：MSB=左像素、1=白/0=黑；cdt 契约 1=黑/0=白，
 *   由 main 的 flush_cb 按行取反转换（P1.3 记录 + INTERFACES §5）。
 * - 未在此处定义的选项回落到 vendor/lvgl/src/lv_conf_internal.h 的默认值
 *   （与模拟器同 vendor 同默认，含 LV_DRAW_BUF_STRIDE_ALIGN=1 → I1@400 行距
 *   恰为 50 字节，与 cdt_frame_t 同布局）。
 */

#ifndef LV_CONF_H
#define LV_CONF_H

/*====================
   颜色 / 内存 / 时基
 *===================*/
#define LV_COLOR_DEPTH 1

#define LV_MEM_SIZE (128 * 1024U)          /* 内置分配器池：128KB（与模拟器同值） */
#define LV_MEM_ADR 0

#define LV_USE_OS LV_OS_NONE               /* 单任务模型：UI 对象只在 ui 任务操作 */

/* 固件时基：esp_timer 毫秒（替代模拟器的 SDL_GetTicks；无 tick 任务）。
 * LVGL 9.3 的 LV_TICK_CUSTOM 约定见 lv_conf_internal.h。 */
#define LV_TICK_CUSTOM 1
#define LV_TICK_CUSTOM_INCLUDE "esp_timer.h"
#define LV_TICK_CUSTOM_SYS_TIME_EXPR ((uint32_t)(esp_timer_get_time() / 1000LL))

/*====================
   宿主设备驱动（固件无 SDL/桌面栈）
 *===================*/
#define LV_USE_SDL 0
#define LV_USE_DRAW_SDL 0

/*====================
   精简功能面（体积/构建时间；与模拟器同面）
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
   主题 / 字体 / 布局（渲染对齐关键项，禁止单端改动）
 *===================*/
#define LV_USE_THEME_DEFAULT 1
#define LV_USE_THEME_MONO 0
#define LV_USE_THEME_SIMPLE 0

/* wqy 点阵落地（A2，2026-09-11）：全量 BDF 位图 1.2MB > 2^20，必须开 LARGE
 * （bitmap_index :20 位域 → uint32_t），否则 >1MB 偏移静默回绕渲染错乱。
 * 两端 lv_conf 关键项一致（P4.3 纪律），本项与 MONTSERRAT 开关同批修改。 */
#define LV_FONT_FMT_TXT_LARGE 1

#define LV_FONT_MONTSERRAT_14 1            /* 模拟器叠加标签（sim main.c）；共享 UI 已换 wqy 点阵 */
#define LV_FONT_MONTSERRAT_16 0            /* 正文已换 cdt_font_wqy_16（wqy 点阵落地）——停用省 flash */
#define LV_FONT_MONTSERRAT_20 0
#define LV_FONT_MONTSERRAT_28 1            /* 主状态词 28px（P2.1；§6 主状态 28-36px） */

#define LV_USE_FLEX 1
#define LV_USE_GRID 0

#endif /*LV_CONF_H*/
