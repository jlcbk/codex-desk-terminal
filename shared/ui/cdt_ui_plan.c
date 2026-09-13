/*
 * cdt_ui_plan.c — PLAN 页（P2.2，A2；ZC7 编号 + 当前步详情面板）
 *
 * §6 PLAN 行：当前任务（选中线程）的步骤、完成数（只数 completed）、当前
 * 步骤标记；长计划分页（4 步/子页，nav.plan_page 切片）；无计划显示
 * "NO PLAN"（暂无计划；ASCII 字体下用英文等义文案，CJK 字体归后续任务）。
 * 步骤标记：[x]=completed、[>]=in_progress（当前步骤反白强调）、[ ]=pending。
 * ZC7（效果图 4 对齐）：每步行首加 1 基编号（跨子页连续，1-8）；底部新增
 * 当前步详情面板——细边框盒内显示当前 in_progress 步骤完整文本（无则首个
 * pending；都没有 → 面板隐藏），盒右下角 "n / total"。列表行与面板几何
 * 位置恒定（面板隐藏不回流）。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define PLAN_ROW_H 30
#define PLAN_ROW_Y0 88
#define PLAN_DETAIL_Y 212   /* 详情面板（4 行列表 88..208 之下、底栏 266 之上） */
#define PLAN_DETAIL_H 50

typedef struct {
    lv_obj_t *root;
    lv_obj_t *sub_ind;
    lv_obj_t *banner;
    lv_obj_t *banner_label;
    lv_obj_t *header;    /* "PLAN 3/14"（+ TRUNCATED） */
    lv_obj_t *rows[CDT_VIEW_ROWS_PER_PAGE];
    lv_obj_t *row_num[CDT_VIEW_ROWS_PER_PAGE];  /* ZC7：1 基编号（跨子页连续） */
    lv_obj_t *row_mark[CDT_VIEW_ROWS_PER_PAGE]; /* [x]/[>]/[ ] */
    lv_obj_t *row_text[CDT_VIEW_ROWS_PER_PAGE];
    lv_obj_t *detail_panel; /* ZC7：当前步详情边框盒（含文本 + n/total） */
    lv_obj_t *detail_text;  /* 当前步完整文本（2 行 wrap） */
    lv_obj_t *detail_n;     /* 右下角 "n / total" */
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

        pl.row_num[i] = cdt_uii_text(pl.rows[i], F_BAR, 2, 6, 20, 18);
        lv_obj_set_style_text_align(pl.row_num[i], LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

        pl.row_mark[i] = cdt_uii_text(pl.rows[i], F_BAR, 24, 6, 40, 18);

        pl.row_text[i] = cdt_uii_text(pl.rows[i], F_BAR, 64, 6, 316, 18);
        lv_label_set_long_mode(pl.row_text[i], LV_LABEL_LONG_DOT);

        lv_obj_add_flag(pl.rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    /* ---- ZC7：当前步详情面板（效果图 4）：细边框盒（1px），内含当前步
     * 完整文本（272px 宽 2 行 wrap，presenter 按 66 列预算截断）与右下角
     * "n / total"；无当前步 → 整盒隐藏（位置恒定，不回流）。 ---- */
    pl.detail_panel = lv_obj_create(pl.root);
    cdt_uii_box(pl.detail_panel, 8, PLAN_DETAIL_Y, 384, PLAN_DETAIL_H);
    lv_obj_set_style_border_color(pl.detail_panel, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_border_width(pl.detail_panel, 1, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(pl.detail_panel, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_clear_flag(pl.detail_panel, LV_OBJ_FLAG_SCROLLABLE);

    pl.detail_text = cdt_uii_text(pl.detail_panel, F_BAR, 6, 4, 272, 42);

    pl.detail_n = cdt_uii_text(pl.detail_panel, F_BAR, 282, 28, 96, 18);
    lv_obj_set_style_text_align(pl.detail_n, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    lv_obj_add_flag(pl.detail_panel, LV_OBJ_FLAG_HIDDEN);

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
        lv_obj_add_flag(pl.detail_panel, LV_OBJ_FLAG_HIDDEN); /* 无当前步详情 */
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
                char num[8];

                /* ZC7：1 基编号，跨子页连续（第 2 子页首行 = 5） */
                snprintf(num, sizeof(num), "%u", (unsigned)(idx + 1u));
                cdt_uii_set_text(pl.row_num[i], num);

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

        /* A0：顶栏时钟前缀（效果图 4 顶栏时间位） */
        if (view->clock_text[0] != '\0') {
            snprintf(buf, sizeof(buf), "%s  %u/%u", view->clock_text,
                     (unsigned)(sub + 1u), (unsigned)pages);
        }
        else {
            snprintf(buf, sizeof(buf), "%u/%u", (unsigned)(sub + 1u), (unsigned)pages);
        }
        cdt_uii_set_text(pl.sub_ind, buf);
        cdt_uii_page_ind_set(pl.page_ind, "PLAN", sub + 1, pages);
    }

    /* ---- ZC7：当前步详情面板（效果图 4）：in_progress 优先、无则首 pending
     * （presenter 裁决 present）；完整文本 2 行 wrap，右下角 "n / total"。 ---- */
    if (view->plan_current_present) {
        char cur_buf[CDT_VIEW_PLAN_CURRENT_BYTES];

        cdt_ui_ascii_safe(cur_buf, sizeof(cur_buf), view->plan_current_text);
        lv_label_set_text(pl.detail_text, cur_buf);

        if (view->plan_total > 0) {
            snprintf(buf, sizeof(buf), "%u / %u",
                     (unsigned)(view->plan_current_index + 1u),
                     (unsigned)view->plan_total);
            cdt_uii_set_text(pl.detail_n, buf);
        }
        else {
            cdt_uii_set_text(pl.detail_n, "");
        }
        lv_obj_remove_flag(pl.detail_panel, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(pl.detail_panel, LV_OBJ_FLAG_HIDDEN);
    }

    lv_label_set_text(pl.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
