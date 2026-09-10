/*
 * cdt_ui_internal.h — 页面模块内部共用小工具（P2.2，A2）
 *
 * 只被 shared/ui 目录内 .c 文件 include（不属于公共 API）；允许 lvgl（UI 层职责），
 * 仍禁 ESP/SDL。约定：400x300、外边距 8、标题栏 28、底栏 24（§6 布局初值），
 * 纯黑白两色；文本一律经 cdt_ui_ascii_safe（非 ASCII → '?'，不静默缺字）。
 */
#ifndef CDT_UI_INTERNAL_H
#define CDT_UI_INTERNAL_H

#include <stddef.h>

#include "cdt_ui.h"

/* 页面字体（内置 Montserrat，ASCII） */
#define F_TITLE  (&lv_font_montserrat_16)
#define F_STATUS (&lv_font_montserrat_28)
#define F_BODY   (&lv_font_montserrat_16)
#define F_BAR    (&lv_font_montserrat_14)

/* 黑底文本 label（parent 内，默认空文本）。
 * 注意：后续若对返回值调用 cdt_uii_box（remove_style_all），字体样式会被
 * 清除——定位文本请改用 cdt_uii_text（样式后置，顺序安全）。 */
lv_obj_t *cdt_uii_label(lv_obj_t *parent, const lv_font_t *font);

/* 文本 label 一体化：创建 → remove_style_all → 定位/尺寸 → 再设字体颜色。
 * 顺序安全（字体样式不会被布局步骤清除）；wd>0 时同时设置宽度（配合 dot 模式）。*/
lv_obj_t *cdt_uii_text(lv_obj_t *parent, const lv_font_t *font, int x, int y,
                       int wd, int ht);

/* remove_style_all + 定位/尺寸（布局原子） */
void cdt_uii_box(lv_obj_t *o, int x, int y, int wd, int ht);

/* 整屏页面根容器：400×300、透明、不可滚动、创建后隐藏。
 * 各页 widgets 挂在自己根容器下（相对坐标 = 绝对坐标）。 */
lv_obj_t *cdt_uii_page_root(void);

/* 链路提示条（反白横幅，几何同 NOW 页：x=8,y=36,384×20）。返回容器，
 * *label_out 返回其白字 label。fresh 时由 apply 隐藏。 */
lv_obj_t *cdt_uii_link_banner(lv_obj_t *parent, lv_obj_t **label_out);

/* 按 view 的链路提示位刷新横幅（disconnected > stale > 隐藏；
 * 文案与 P2.1 NOW 页一致）。 */
void cdt_uii_link_banner_apply(lv_obj_t *bar, lv_obj_t *label, const cdt_view_t *view);

/* 底栏：分隔线 + 页面指示（左）+ 静音文字标签（右）。 */
void cdt_uii_bottom_bar(lv_obj_t *parent, lv_obj_t **page_ind, lv_obj_t **mute);

/* 页面指示文本："PAGE: <page>"（subs>1 时追加 " <i>/<n>"，i 从 1 计）。 */
void cdt_uii_page_ind_set(lv_obj_t *label, const char *page, int sub, int subs);

/* view 文本 → ASCII 安全 → 写入 label（缓冲由本函数持有）。 */
void cdt_uii_set_text(lv_obj_t *label, const char *view_text);

/* 标题栏：左标题 + 右副文本（如子页指示）。 */
void cdt_uii_title_row(lv_obj_t *parent, const char *title, lv_obj_t **title_out,
                       lv_obj_t **right_out);

#endif /* CDT_UI_INTERNAL_H */
