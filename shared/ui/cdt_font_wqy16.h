/*
 * cdt_font_wqy16.h — 文泉驿点阵宋 1bpp 字体（生成文件，勿手改）
 * 真源、字符集与 fallback 链见 cdt_font_wqy16.c 头注释；
 * 契约：docs/VERSIONS.md 字体行、tests/SCENARIOS.md §5.3（不静默缺字）。
 */
#ifndef CDT_WQY_CDT_FONT_WQY_16_H
#define CDT_WQY_CDT_FONT_WQY_16_H

#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 正文/紧凑唯一字体档（activity/attention/plan/usage/项目名/底栏/行列表/提示条） */
extern const lv_font_t cdt_font_wqy_16;

/* 码点是否在本字体覆盖范围内（cdt_ui_ascii_safe 放行判断） */
bool cdt_font_wqy16_covers(uint32_t codepoint);

#ifdef __cplusplus
}
#endif

#endif /* CDT_WQY_CDT_FONT_WQY_16_H */
