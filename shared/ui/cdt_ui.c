/*
 * cdt_ui.c — UI 入口：ui_init / ui_apply / ui_apply_nav / ui_key（P2.1–P2.3，A2）
 *
 * P2.2/P2.3 范围：五页调度（NOW/AGENTS/PLAN/USAGE/LOW BATTERY）、导航状态
 * 缓存、KEY 入口。页面选择真源在宿主 DeviceRuntime.selected_page；本层缓存
 * 导航副本仅为 cdt_ui_apply(view) / cdt_ui_key(ev) 便捷路径服务。
 * ui_init 假定调用方已完成 lv_init 并创建了 LVGL display（活动屏幕即页面根）。
 */
#include <stdio.h>

#include "cdt_font_wqy16.h"
#include "cdt_ui_internal.h"
#include "cdt_ui_now.h"
#include "cdt_ui_pages.h"

static cdt_nav_t g_nav;        /* 导航缓存（apply/key 便捷路径） */
static cdt_view_t g_last_view; /* 最近一次 apply 的 ViewModel（key 依赖页数/forced） */
static bool g_have_view;

static void set_page_visible(cdt_page_t page)
{
    lv_obj_t *roots[6] = { NULL }; /* 以 cdt_page_t 枚举值索引 */

    roots[CDT_PAGE_NOW] = cdt_ui_now_root();
    roots[CDT_PAGE_AGENTS] = cdt_ui_agents_root();
    roots[CDT_PAGE_PLAN] = cdt_ui_plan_root();
    roots[CDT_PAGE_USAGE] = cdt_ui_usage_root();
    roots[CDT_PAGE_LOW_BATTERY] = cdt_ui_lowbat_root();

    if (page <= CDT_PAGE_INVALID || page > CDT_PAGE_LOW_BATTERY) {
        page = CDT_PAGE_NOW; /* 防御 */
    }
    for (int i = 1; i <= CDT_PAGE_LOW_BATTERY; i++) {
        lv_obj_t *r = roots[i];
        if (r == NULL) continue;
        if ((cdt_page_t)i == page) lv_obj_remove_flag(r, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(r, LV_OBJ_FLAG_HIDDEN);
    }
}

void cdt_ui_init(void)
{
    lv_obj_t *scr = lv_screen_active();

    /* 页面字体（全量 wqy 单字体，见 cdt_ui_internal.h F_*）为编译期
     * 常量 lv_font_t，无需运行期初始化；直接构建页面。 */

    /* 页面根：白底（I1 索引 1；SDL 驱动 1=白 0=黑），无边框无滚动 */
    lv_obj_set_style_bg_color(scr, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(scr, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(scr, 0, LV_PART_MAIN);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    cdt_ui_now_create();
    cdt_ui_agents_create();
    cdt_ui_plan_create();
    cdt_ui_usage_create();
    cdt_ui_lowbat_create();

    cdt_nav_init(&g_nav, CDT_PAGE_NOW);
    g_have_view = false;
    set_page_visible(CDT_PAGE_NOW);
}

void cdt_ui_apply_nav(const cdt_view_t *view, const cdt_nav_t *nav)
{
    if (view == NULL) return;

    g_last_view = *view;
    g_have_view = true;
    if (nav != NULL) g_nav = *nav;
    cdt_nav_clamp(&g_nav, view);

    set_page_visible(view->page);
    switch (view->page) {
        case CDT_PAGE_AGENTS:
            cdt_ui_agents_apply(view, &g_nav);
            break;
        case CDT_PAGE_PLAN:
            cdt_ui_plan_apply(view, &g_nav);
            break;
        case CDT_PAGE_USAGE:
            cdt_ui_usage_apply(view);
            break;
        case CDT_PAGE_LOW_BATTERY:
            cdt_ui_lowbat_apply(view);
            break;
        default: /* NOW 与防御路径 */
            cdt_ui_now_apply(view);
            break;
    }
}

void cdt_ui_apply(const cdt_view_t *view)
{
    cdt_ui_apply_nav(view, NULL); /* 用缓存导航 */
}

cdt_page_t cdt_ui_nav_page(void)
{
    return g_nav.page;
}

uint32_t cdt_ui_key(cdt_key_event_t ev)
{
    uint32_t act;

    act = cdt_nav_key(&g_nav, g_have_view ? &g_last_view : NULL, ev);
    if (g_have_view) {
        cdt_ui_apply_nav(&g_last_view, &g_nav); /* 立即重渲染（子页推进/静音标签） */
    }
    /* 宿主据 act 把 g_nav.page（cdt_ui_nav_page()）写回 runtime.selected_page、
     * 记录静音；下轮 present+apply 收敛。 */
    return act;
}

/*
 * 显示兜底（字体栈定稿 2026-09-11：全量 wqy 单字体、无 fallback 字体层）：
 * wqy BDF 全量收编的码点（CJK 统一+扩展 A+假名+韩文+希腊/西里尔+Latin，
 * 见 cdt_font_wqy16.c 头注释）按 UTF-8 序列原样放行；wqy 没有的码点
 * （emoji 等）每个折为一个可见占位符 '?'，不静默缺字（§6）。
 * 控制字符折为空格。dst 与 src 可不重叠；dstsz 含结尾 NUL。
 */
void cdt_ui_ascii_safe(char *dst, size_t dstsz, const char *src)
{
    const unsigned char *p = (const unsigned char *)(src ? src : "");
    size_t out = 0;

    if (dst == NULL || dstsz == 0) return;

    while (*p != '\0' && out + 1 < dstsz) {
        unsigned char b = *p;
        size_t skip = 1;
        bool covered = false;

        if (b < 0x20u) {
            dst[out++] = ' '; /* 控制字符 → 空格，保护单行布局 */
        }
        else if (b < 0x80u) {
            dst[out++] = (char)b; /* ASCII 原样（wqy 点阵自带 Latin 可渲染） */
        }
        else {
            /* 非 ASCII：解码码点，子集已覆盖 → 原样放行整个序列；
             * 未覆盖（emoji/假名等）→ 一个 '?' 占位（S17 可见替代符） */
            uint32_t cp = 0xFFFFFFFFu;
            if ((b & 0xE0u) == 0xC0u) { skip = 2; cp = b & 0x1Fu; }
            else if ((b & 0xF0u) == 0xE0u) { skip = 3; cp = b & 0x0Fu; }
            else if ((b & 0xF8u) == 0xF0u) { skip = 4; cp = b & 0x07u; }
            {
                size_t i;
                bool bad = (cp == 0xFFFFFFFFu);
                for (i = 1; !bad && i < skip; i++) {
                    if ((p[i] & 0xC0u) != 0x80u) bad = true;
                    else cp = (cp << 6) | (uint32_t)(p[i] & 0x3Fu);
                }
                covered = !bad && cdt_font_wqy16_covers(cp);
            }
            if (covered) {
                size_t i;
                for (i = 0; i < skip && *p != '\0' && out + 1 < dstsz; i++) {
                    dst[out++] = (char)*p++;
                }
                continue; /* p 已前移，跳过公共推进 */
            }
            dst[out++] = '?';
        }
        while (skip > 1 && *p != '\0') { p++; skip--; }
        if (*p != '\0') p++;
    }
    dst[out] = '\0';
}
