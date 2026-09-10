/*
 * cdt_ui.c — UI 入口：ui_init / ui_apply / ui_key（P2.1，A2）
 *
 * P2.1 范围：NOW 单页（cdt_ui_now.c）；页面调度、KEY 导航归 P2.2。
 * ui_init 假定调用方已完成 lv_init 并创建了 LVGL display（活动屏幕即页面根）。
 */
#include <stdio.h>

#include "cdt_ui.h"
#include "cdt_ui_now.h"

void cdt_ui_init(void)
{
    lv_obj_t *scr = lv_screen_active();

    /* 页面根：白底（I1 索引 1；SDL 驱动 1=白 0=黑），无边框无滚动 */
    lv_obj_set_style_bg_color(scr, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(scr, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(scr, 0, LV_PART_MAIN);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    cdt_ui_now_create();
}

void cdt_ui_apply(const cdt_view_t *view)
{
    if (view == NULL) return;
    cdt_ui_now_apply(view);
}

void cdt_ui_key(cdt_key_event_t ev)
{
    /* P2.1 桩：仅 NOW 单页。
     * - 短按：四页轮换 NOW→AGENTS→PLAN→USAGE（P2.2，需 Runtime.selected_page）；
     * - 长按：翻转当前提醒静音位（P2.2，需写 DeviceRuntime.muted_attention_id；
     *   ACK 只表示本地静音，绝不等于批准 Codex 操作）。
     * 此处不持有 Runtime，也不允许 UI 直接改业务状态——接线在 P2.2 经宿主注入。 */
    (void)ev;
}

void cdt_ui_ascii_safe(char *dst, size_t dstsz, const char *src)
{
    const unsigned char *p = (const unsigned char *)(src ? src : "");
    size_t out = 0;

    if (dst == NULL || dstsz == 0) return;

    while (*p != '\0' && out + 1 < dstsz) {
        unsigned char b = *p;
        size_t skip = 1;

        if (b < 0x20u) {
            dst[out++] = ' '; /* 控制字符 → 空格，保护单行布局 */
        }
        else if (b < 0x80u) {
            dst[out++] = (char)b; /* ASCII 原样（内置 Montserrat 可渲染） */
        }
        else {
            /* 非 ASCII 码点 → 一个 '?' 占位；跳过整个 UTF-8 序列 */
            dst[out++] = '?';
            if ((b & 0xE0u) == 0xC0u) skip = 2;
            else if ((b & 0xF0u) == 0xE0u) skip = 3;
            else if ((b & 0xF8u) == 0xF0u) skip = 4;
        }
        while (skip > 1 && *p != '\0') { p++; skip--; }
        if (*p != '\0') p++;
    }
    dst[out] = '\0';
}
