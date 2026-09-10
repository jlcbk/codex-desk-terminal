/*
 * cdt_font_noto_sc.h — Noto Sans SC 子集字体（生成文件，勿手改）
 * 真源与范围见 cdt_font_noto_sc.c 头注释；契约：docs/VERSIONS.md 字体行、
 * tests/SCENARIOS.md §5.3（固定字体并记录范围，不静默缺字）。
 */
#ifndef CDT_FONT_NOTO_SC_H
#define CDT_FONT_NOTO_SC_H

#include <stdint.h>

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 14px / 16px 子集字体（A4；与 Montserrat 14/16 同为行内回退备胎） */
extern const lv_font_t cdt_font_noto_sc_14;
extern const lv_font_t cdt_font_noto_sc_16;

/* 码点是否在子集覆盖范围内（cdt_ui_ascii_safe 放行已覆盖 CJK） */
bool cdt_font_noto_sc_covers(uint32_t codepoint);

#ifdef __cplusplus
}
#endif

#endif /* CDT_FONT_NOTO_SC_H */
