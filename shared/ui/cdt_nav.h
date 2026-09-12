/*
 * cdt_nav.h — KEY 导航纯逻辑（P2.2，A2）
 *
 * 契约：docs/DEVELOPMENT_PLAN.md §6 KEY 初值行（短按轮换
 * NOW→AGENTS→PLAN→USAGE→DETAILS（ZC4 v1.2 五页）；
 * AGENTS/PLAN 超一页先推进子页再切换主页面，P2 固定该行为；长按只静音当前提醒，
 * 新 pending 可再次提醒）；docs/INTERFACES.md §4（DeviceRuntime.selected_page
 * 归设备本地；电池强制页不可被普通页切换覆盖；恢复健康连接保留当前普通页面）。
 *
 * 边界：本模块是纯逻辑（无 LVGL/SDL/ESP、无 IO），宿主（模拟器/固件 main）
 * 持有 cdt_nav_t 实例，并把返回的宿主动作写回 DeviceRuntime：
 *   - CDT_NAV_ACT_PAGE → runtime.selected_page = nav.page
 *   - CDT_NAV_ACT_MUTE → 把当前提醒标识写入 runtime.muted_attention_id
 *     （ACK 只表示本地静音/已读，绝不等于批准 Codex 操作）
 * LOW_BATTERY 页由 Presenter 按电源态仲裁产出，绝不进入 nav.page。
 *
 * 纯 C99；被主机端 C 断言测试（tests/shared/test_pages.c）直接覆盖。
 */
#ifndef CDT_NAV_H
#define CDT_NAV_H

#include <stdint.h>

#include "../presenter/cdt_view.h"

#ifdef __cplusplus
extern "C" {
#endif

/* KEY 事件（设备端 KEY；模拟器由 SDL 键盘映射产生）。
 * 定义于本可移植头（cdt_ui.h 转发），nav 纯逻辑不引入 LVGL。 */
typedef enum {
    CDT_KEY_SHORT_PRESS = 1, /* 短按：子页推进 / 页面轮换 */
    CDT_KEY_LONG_PRESS = 2   /* 长按：静音当前提醒（不切页、不解除强制页） */
} cdt_key_event_t;

/* 宿主动作位掩码（cdt_nav_key 返回） */
#define CDT_NAV_ACT_NONE ((uint32_t)0)
#define CDT_NAV_ACT_PAGE (((uint32_t)1) << 0) /* nav.page 变化：宿主写回 runtime */
#define CDT_NAV_ACT_MUTE (((uint32_t)1) << 1) /* 长按：宿主记录静音当前提醒 */

typedef struct {
    cdt_page_t page; /* 普通页选择（永不为 LOW_BATTERY/INVALID） */
    uint8_t agents_page; /* AGENTS 子页 0-based */
    uint8_t plan_page;   /* PLAN 子页 0-based */
} cdt_nav_t;

/* 初始化：普通页从 page 起（深睡重启回 NOW → 宿主传 CDT_PAGE_NOW）。*/
void cdt_nav_init(cdt_nav_t *nav, cdt_page_t page);

/*
 * 处理一次 KEY 事件（view 为当前 ViewModel，可为 NULL）。
 * 固定行为（P2.2，计划 §6）：
 *   - view.low_battery_forced：短按被拒（返回 NONE，nav 不变——低压页不可被
 *     普通页切换覆盖）；长按仍返回 MUTE（静音不解除强制页）。
 *   - 短按 AGENTS：还有子页 → agents_page++（返回 NONE，宿主重渲染即可）；
 *     否则切 PLAN（plan_page 清零，返回 PAGE）。
 *   - 短按 PLAN：同上；末子页 → 切 USAGE。
 *   - 短按 NOW → AGENTS；短按 USAGE → DETAILS；短按 DETAILS → NOW
 *     （ZC4 v1.2：普通页循环 NOW→AGENTS→PLAN→USAGE→DETAILS→NOW）。
 *   - 长按：任何页只返回 MUTE，不切页。
 */
uint32_t cdt_nav_key(cdt_nav_t *nav, const cdt_view_t *view, cdt_key_event_t ev);

/*
 * 子页越界钳制（内容收缩后调用：新快照页数变少时把子页拉回有效范围）。
 * 返回是否发生了改动。view 可为 NULL（视为无数据，子页清零）。
 */
bool cdt_nav_clamp(cdt_nav_t *nav, const cdt_view_t *view);

#ifdef __cplusplus
}
#endif

#endif /* CDT_NAV_H */
