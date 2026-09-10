/*
 * cdt_ui.h — 共享 LVGL UI 入口（P2.1 NOW / P2.2+P2.3 全页面，A2）
 *
 * 契约：docs/INTERFACES.md §5 UI 行——ui_init(display) / ui_apply(view) /
 * ui_key(event)；只在 LVGL 所属任务操作对象；无网络/ADC/文件 IO。
 * ViewModel（cdt_view_t）是 UI 唯一输入；页面选择/子页由 cdt_nav_t（纯逻辑）
 * 给出，宿主经 DeviceRuntime.selected_page 持有主页面真源。
 *
 * 边界（任务红线）：
 *   - 本层不调用 ESP-IDF/网络/ADC/文件 IO；
 *   - 不解释 Codex 事件（那是 Bridge/Presenter 的事）；
 *   - P2.2 起五页齐备：NOW / AGENTS / PLAN / USAGE / LOW BATTERY（强制页）。
 */
#ifndef CDT_UI_H
#define CDT_UI_H

#include <stddef.h>

#include "cdt_nav.h"
#include "cdt_view.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* KEY 事件类型（cdt_key_event_t）与宿主动作位由可移植头 cdt_nav.h 定义。 */

/* 在当前活动屏幕上构建全部页面（各页自带整屏根容器，非当前页隐藏）。
 * 此后 ui_apply / ui_apply_nav 只改属性与可见性，不重建对象。 */
void cdt_ui_init(void);

/* 用 ViewModel 刷新 UI；页面选择取 UI 内部缓存的导航状态（默认 NOW）。
 * 宿主需要精确控制子页时用 cdt_ui_apply_nav。 */
void cdt_ui_apply(const cdt_view_t *view);

/* 同上，但显式给出导航状态（主页面 + AGENTS/PLAN 子页）。
 * 强制 LOW BATTERY 页不受 nav 影响：view.low_battery_forced 时按
 * view.page（=LOW_BATTERY）渲染。 */
void cdt_ui_apply_nav(const cdt_view_t *view, const cdt_nav_t *nav);

/*
 * KEY 事件入口（INTERFACES §5 ui_key）：内部对缓存的 view/nav 执行
 * cdt_nav_key 并立即重渲染；返回宿主动作位（CDT_NAV_ACT_*）——宿主据其把
 * nav.page 写回 runtime.selected_page、记录静音。便捷路径；模拟器宿主直接
 * 用 cdt_nav_key + cdt_ui_apply_nav（见 simulator/main.c）。
 */
uint32_t cdt_ui_key(cdt_key_event_t ev);

/* 当前导航缓存的普通页选择（cdt_ui_key 返回 CDT_NAV_ACT_PAGE 时，
 * 宿主把它写回 runtime.selected_page）。 */
cdt_page_t cdt_ui_nav_page(void);

/*
 * 显示兜底：把 src 中的非 ASCII 码点替换为可见占位符 '?'（每码点一个）。
 * 当前只带 ASCII 内置字体（Noto Sans SC 子集待 A0 排期）；§6 要求未知字符
 * 用"可见替代符"，不静默缺字。控制字符同样折为空格。
 * dst 与 src 可不重叠；dstsz 含结尾 NUL。
 */
void cdt_ui_ascii_safe(char *dst, size_t dstsz, const char *src);

#ifdef __cplusplus
}
#endif

#endif /* CDT_UI_H */
