/*
 * cdt_ui_plan.c — PLAN 页（P2.2，A2）
 *
 * §6 PLAN 行：当前任务（选中线程）的步骤、完成数（只数 completed）、当前
 * 步骤标记；长计划分页（4 步/子页，nav.plan_page 切片）；无计划显示
 * "NO PLAN"（暂无计划；ASCII 字体下用英文等义文案，CJK 字体归后续任务）。
 * 步骤标记：[x]=completed、[>]=in_progress（当前步骤反白强调）、[ ]=pending。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define PLAN_ROW_H 34
#define PLAN_ROW_Y0 92

typedef struct {
    lv_obj_t *root;
    lv_obj_t *sub_ind;
    lv_obj_t *banner;
    lv_obj_t *banner_label;
    lv_obj_t *header;    /* "PLAN 3/14"（+ TRUNCATED） */
    lv_obj_t *rows[CDT_VIEW_ROWS_PER_PAGE];
    lv_obj_t *row_mark[CDT_VIEW_ROWS_PER_PAGE]; /* [x]/[>]/[ ] */
    lv_obj_t *row_text[CDT_VIEW_ROWS_PER_PAGE];
    lv_obj_t *empty;     /* "NO PLAN" */
    lv_obj_t *page_ind;
    lv_obj_t *mute;
} plan_widgets_t;

static plan_widgets_t pl;

lv_obj_t *cdt_ui_plan_root(void)
{
    return pl.root;
}

void cdt_ui_plan_create(void)
{
    int i;

    pl.root = cdt_uii_page_root();
    cdt_uii_title_row(pl.root, "PLAN", NULL, &pl.sub_ind);
    pl.banner = cdt_uii_link_banner(pl.root, &pl.banner_label);

    pl.header = cdt_uii_text(pl.root, F_BODY, 8, 62, 384, 22);

    for (i = 0; i < CDT_VIEW_ROWS_PER_PAGE; i++) {
        int y = PLAN_ROW_Y0 + i * PLAN_ROW_H;

        pl.rows[i] = lv_obj_create(pl.root);
        cdt_uii_box(pl.rows[i], 8, y, 384, PLAN_ROW_H);

        pl.row_mark[i] = cdt_uii_text(pl.rows[i], F_BAR, 0, 7, 34, 18);

        pl.row_text[i] = cdt_uii_text(pl.rows[i], F_BAR, 38, 7, 346, 18);
        lv_label_set_long_mode(pl.row_text[i], LV_LABEL_LONG_DOT);

        lv_obj_add_flag(pl.rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    pl.empty = cdt_uii_text(pl.root, F_BODY, 8, 150, 384, 24);
    lv_obj_set_style_text_align(pl.empty, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_label_set_text(pl.empty, "NO PLAN");

    cdt_uii_bottom_bar(pl.root, &pl.page_ind, &pl.mute);
}

void cdt_ui_plan_apply(const cdt_view_t *view, const cdt_nav_t *nav)
{
    uint8_t start;
    int i;
    char buf[CDT_VIEW_STEP_BYTES + 32];

    if (view == NULL || nav == NULL) return;

    cdt_uii_link_banner_apply(pl.banner, pl.banner_label, view);

    if (view->plan_total == 0 && view->plan_step_count == 0) {
        for (i = 0; i < CDT_VIEW_ROWS_PER_PAGE; i++) {
            lv_obj_add_flag(pl.rows[i], LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(pl.empty, LV_OBJ_FLAG_HIDDEN); /* 暂无计划 */
        cdt_uii_set_text(pl.header, "");
        cdt_uii_set_text(pl.sub_ind, "");
        cdt_uii_page_ind_set(pl.page_ind, "PLAN", 1, 1);
        lv_label_set_text(pl.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
        return;
    }

    lv_obj_add_flag(pl.empty, LV_OBJ_FLAG_HIDDEN);

    /* 完成数只数 completed；total 来自数据；截断加标记 */
    if (view->plan_truncated) {
        snprintf(buf, sizeof(buf), "PLAN %u/%u TRUNCATED",
                 (unsigned)view->plan_completed, (unsigned)view->plan_total);
    }
    else {
        snprintf(buf, sizeof(buf), "PLAN %u/%u",
                 (unsigned)view->plan_completed, (unsigned)view->plan_total);
    }
    cdt_uii_set_text(pl.header, buf);

    {
        uint8_t pages = view->plan_pages > 0 ? view->plan_pages : 1;
        uint8_t sub = nav->plan_page;

        start = (uint8_t)(sub * CDT_VIEW_ROWS_PER_PAGE);
        for (i = 0; i < CDT_VIEW_ROWS_PER_PAGE; i++) {
            uint8_t idx = (uint8_t)(start + (uint8_t)i);
            if (idx < view->plan_step_count) {
                const cdt_plan_step_row_t *row = &view->plan_steps[idx];

                switch ((cdt_step_status_t)row->status) {
                    case CDT_STEP_STATUS_COMPLETED:
                        cdt_uii_set_text(pl.row_mark[i], "[x]");
                        lv_obj_set_style_bg_opa(pl.row_text[i], LV_OPA_TRANSP, LV_PART_MAIN);
                        lv_obj_set_style_text_color(pl.row_text[i], lv_color_black(),
                                                    LV_PART_MAIN);
                        break;
                    case CDT_STEP_STATUS_IN_PROGRESS:
                        cdt_uii_set_text(pl.row_mark[i], "[>]"); /* 当前步骤标记 */
                        /* 当前步骤反白强调 */
                        lv_obj_set_style_bg_color(pl.row_text[i], lv_color_black(),
                                                  LV_PART_MAIN);
                        lv_obj_set_style_bg_opa(pl.row_text[i], LV_OPA_COVER, LV_PART_MAIN);
                        lv_obj_set_style_pad_all(pl.row_text[i], 1, LV_PART_MAIN);
                        lv_obj_set_style_text_color(pl.row_text[i], lv_color_white(),
                                                    LV_PART_MAIN);
                        break;
                    default:
                        cdt_uii_set_text(pl.row_mark[i], "[ ]");
                        lv_obj_set_style_bg_opa(pl.row_text[i], LV_OPA_TRANSP, LV_PART_MAIN);
                        lv_obj_set_style_text_color(pl.row_text[i], lv_color_black(),
                                                    LV_PART_MAIN);
                        break;
                }
                cdt_uii_set_text(pl.row_text[i], row->text);
                lv_obj_remove_flag(pl.rows[i], LV_OBJ_FLAG_HIDDEN);
            }
            else {
                lv_obj_add_flag(pl.rows[i], LV_OBJ_FLAG_HIDDEN);
            }
        }

        snprintf(buf, sizeof(buf), "%u/%u", (unsigned)(sub + 1u), (unsigned)pages);
        cdt_uii_set_text(pl.sub_ind, buf);
        cdt_uii_page_ind_set(pl.page_ind, "PLAN", sub + 1, pages);
    }

    lv_label_set_text(pl.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
