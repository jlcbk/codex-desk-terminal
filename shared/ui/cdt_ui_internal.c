/*
 * cdt_ui_internal.c — 页面模块内部共用小工具实现（P2.2，A2）
 */
#include <stdio.h>

#include "cdt_ui_internal.h"

lv_obj_t *cdt_uii_label(lv_obj_t *parent, const lv_font_t *font)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_color(l, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_text_font(l, font, LV_PART_MAIN);
    lv_label_set_text(l, "");
    return l;
}

lv_obj_t *cdt_uii_text(lv_obj_t *parent, const lv_font_t *font, int x, int y,
                       int wd, int ht)
{
    lv_obj_t *l = lv_label_create(parent);

    lv_obj_remove_style_all(l);
    lv_obj_set_pos(l, (int32_t)x, (int32_t)y);
    lv_obj_set_size(l, (int32_t)wd, (int32_t)ht);
    /* 字体/颜色必须在 remove_style_all 之后设置（顺序安全的本义） */
    lv_obj_set_style_text_color(l, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_text_font(l, font, LV_PART_MAIN);
    lv_label_set_text(l, "");
    return l;
}

void cdt_uii_box(lv_obj_t *o, int x, int y, int wd, int ht)
{
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, (int32_t)x, (int32_t)y);
    lv_obj_set_size(o, (int32_t)wd, (int32_t)ht);
}

lv_obj_t *cdt_uii_page_root(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_t *root = lv_obj_create(scr);

    cdt_uii_box(root, 0, 0, 400, 300);
    lv_obj_clear_flag(root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(root, LV_OBJ_FLAG_HIDDEN); /* 由 cdt_ui.c 调度显示 */
    return root;
}

lv_obj_t *cdt_uii_link_banner(lv_obj_t *parent, lv_obj_t **label_out)
{
    lv_obj_t *bar = lv_obj_create(parent);

    cdt_uii_box(bar, 8, 36, 384, 20);
    lv_obj_set_style_bg_color(bar, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(bar, 0, LV_PART_MAIN);

    *label_out = cdt_uii_label(bar, F_BAR);
    lv_obj_set_style_text_color(*label_out, lv_color_white(), LV_PART_MAIN);
    lv_obj_center(*label_out);
    lv_obj_add_flag(bar, LV_OBJ_FLAG_HIDDEN);
    return bar;
}

void cdt_uii_link_banner_apply(lv_obj_t *bar, lv_obj_t *label, const cdt_view_t *view)
{
    if (view->link_disconnected) {
        lv_label_set_text(label, "LINK DISCONNECTED - TIME FROZEN");
        lv_obj_remove_flag(bar, LV_OBJ_FLAG_HIDDEN);
    }
    else if (view->link_stale) {
        lv_label_set_text(label, "LINK STALE - TIME FROZEN");
        lv_obj_remove_flag(bar, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(bar, LV_OBJ_FLAG_HIDDEN);
    }
}

void cdt_uii_bottom_bar(lv_obj_t *parent, lv_obj_t **page_ind, lv_obj_t **mute)
{
    lv_obj_t *rule = lv_obj_create(parent);

    cdt_uii_box(rule, 8, 266, 384, 2);
    lv_obj_set_style_bg_color(rule, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(rule, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(rule, 0, LV_PART_MAIN);

    *page_ind = cdt_uii_text(parent, F_BAR, 8, 271, 200, 18);

    *mute = cdt_uii_text(parent, F_BAR, 242, 271, 150, 18);
    lv_obj_set_style_text_align(*mute, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);
}

void cdt_uii_page_ind_set(lv_obj_t *label, const char *page, int sub, int subs)
{
    char buf[48];

    if (subs > 1) {
        snprintf(buf, sizeof(buf), "PAGE: %s %d/%d", page, sub, subs);
    }
    else {
        snprintf(buf, sizeof(buf), "PAGE: %s", page);
    }
    cdt_uii_set_text(label, buf);
}

void cdt_uii_set_text(lv_obj_t *label, const char *view_text)
{
    char buf[CDT_VIEW_STEP_BYTES + 24];
    cdt_ui_ascii_safe(buf, sizeof(buf), view_text);
    lv_label_set_text(label, buf);
}

void cdt_uii_title_row(lv_obj_t *parent, const char *title, lv_obj_t **title_out,
                       lv_obj_t **right_out)
{
    /* 标题 label 总是创建（页面名必须可见）；指针按需返回 */
    lv_obj_t *t = cdt_uii_text(parent, F_TITLE, 8, 8, 200, 24);
    lv_label_set_text(t, title);
    if (title_out != NULL) *title_out = t;

    if (right_out != NULL) {
        *right_out = cdt_uii_text(parent, F_BAR, 232, 12, 160, 18);
        lv_obj_set_style_text_align(*right_out, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);
    }
}
