/*
 * cdt_ui_now.h — NOW 页（P2.1，A2）
 *
 * 契约：docs/DEVELOPMENT_PLAN.md §6 NOW 行 + docs/INTERFACES.md §5。
 * 布局初值：400×300、外边距 8px、标题栏 28px、底栏 24px、主状态 28px、正文 16px。
 */
#ifndef CDT_UI_NOW_H
#define CDT_UI_NOW_H

#include "cdt_view.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 在当前活动屏幕构建 NOW 页静态布局（固定坐标，不随内容回流）。*/
void cdt_ui_now_create(void);

/* 按 ViewModel 刷新 NOW 页（文本/可见性/黑白强调样式）。*/
void cdt_ui_now_apply(const cdt_view_t *view);

#ifdef __cplusplus
}
#endif

#endif /* CDT_UI_NOW_H */
