/*
 * cdt_ui_pages.h — P2.2/P2.3 新增页面接口（A2）
 *
 * AGENTS / PLAN / USAGE（P2.2）与 LOW BATTERY 强制页（P2.3）。
 * 每页：cdt_ui_<p>_create() 构建静态布局（自带整屏根容器，初始隐藏），
 * cdt_ui_<p>_apply(view[, nav]) 按 ViewModel+导航刷新。可见性由 cdt_ui.c
 * 调度（经 root 访问器），页面本身不持导航状态。
 */
#ifndef CDT_UI_PAGES_H
#define CDT_UI_PAGES_H

#include "cdt_nav.h"
#include "cdt_view.h"
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- AGENTS（§6：排序、最多 4 行/页、总数/裁剪标记、选中任务生成其他页）---- */
void cdt_ui_agents_create(void);
void cdt_ui_agents_apply(const cdt_view_t *view, const cdt_nav_t *nav);
lv_obj_t *cdt_ui_agents_root(void);

/* ---- PLAN（§6：步骤、完成数、当前步骤标记、长计划分页、无计划提示）---- */
void cdt_ui_plan_create(void);
void cdt_ui_plan_apply(const cdt_view_t *view, const cdt_nav_t *nav);
lv_obj_t *cdt_ui_plan_root(void);

/* ---- USAGE（§6：实际窗口长度、usedPercent、reset 倒计时、context）---- */
void cdt_ui_usage_create(void);
void cdt_ui_usage_apply(const cdt_view_t *view);
lv_obj_t *cdt_ui_usage_root(void);

/* ---- DETAILS（ZC4 v1.2 第 6 屏：选中会话的 PROJECT/MODEL/STATUS/DURATION/
 * CONTEXT/INPUT/OUTPUT/CACHED 明细行；缺值 "--"，不编造）---- */
void cdt_ui_details_create(void);
void cdt_ui_details_apply(const cdt_view_t *view);
lv_obj_t *cdt_ui_details_root(void);

/* ---- LOW BATTERY 强制页（P2.3：电压、可用电量、低压提示、充电/唤醒说明）---- */
void cdt_ui_lowbat_create(void);
void cdt_ui_lowbat_apply(const cdt_view_t *view);
lv_obj_t *cdt_ui_lowbat_root(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_UI_PAGES_H */
