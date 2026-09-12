/*
 * cdt_ui_agents.c — AGENTS 页（P2.2，A2；ZC5 行尾时长；ZC8 两行式布局）
 *
 * §6 AGENTS 行：NEEDS YOU→ERROR→WORKING/THINKING→DONE→IDLE 排序（排序在
 * presenter 完成）；ZC8 两行式（效果图 3）：
 *   第一行 = 状态徽标（needs_you/error 反白强调）+ 项目名 + 右上 elapsed
 *            （mm:ss / hh:mm:ss，终态定格，ZC5）；
 *   第二行 = 活动文本（缩进、presenter 已按列预算截断；activity 为空 →
 *            该行省略，不占位）。
 * 每页 3 行（两行式占高；CDT_VIEW_AGENTS_ROWS_PER_PAGE，子页切片由
 * nav.agents_page 选择）；顶栏右侧 "SESSIONS <threads_total>"（总数语义，
 * 翻页指示仍在底栏 PAGE: AGENTS i/n）；底部 TOTAL 行（含 "+N MORE" 裁剪
 * 标记）+ 提示行 "PRESS KEY: DETAILS"（纯提示；KEY 实际行为不变）。
 * 布局：标题 28 / 链路条 20 / 3×48px 两行式行 / TOTAL 行 / 提示行 / 底栏 24。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define AGENTS_ROW_H 48
#define AGENTS_ROW_Y0 64
#define AGENTS_ROWS CDT_VIEW_AGENTS_ROWS_PER_PAGE

typedef struct {
    lv_obj_t *root;
    lv_obj_t *sub_ind;   /* 标题右：SESSIONS <总数>（ZC8） */
    lv_obj_t *banner;    /* 链路提示条 */
    lv_obj_t *banner_label;
    lv_obj_t *rows[AGENTS_ROWS];                  /* 每行容器（两行式） */
    lv_obj_t *row_mark[AGENTS_ROWS];   /* 行首 "!" 等待提醒标记 */
    lv_obj_t *row_state[AGENTS_ROWS];  /* 行左：状态词 */
    lv_obj_t *row_project[AGENTS_ROWS];/* 行中：项目名 */
    lv_obj_t *row_elapsed[AGENTS_ROWS];/* 行右：右对齐 elapsed（ZC5） */
    lv_obj_t *row_activity[AGENTS_ROWS];/* ZC8 第二行：活动文本（空 → 隐藏） */
    lv_obj_t *trunc;     /* 裁剪/总数行 */
    lv_obj_t *hint;      /* ZC8：底部操作提示行 */
    lv_obj_t *empty;     /* 无数据提示 */
    lv_obj_t *page_ind;
    lv_obj_t *mute;
} agents_widgets_t;

static agents_widgets_t ag;

lv_obj_t *cdt_ui_agents_root(void)
{
    return ag.root;
}

void cdt_ui_agents_create(void)
{
    int i;

    ag.root = cdt_uii_page_root();
    cdt_uii_title_row(ag.root, "AGENTS", NULL, &ag.sub_ind);
    ag.banner = cdt_uii_link_banner(ag.root, &ag.banner_label);

    for (i = 0; i < AGENTS_ROWS; i++) {
        int y = AGENTS_ROW_Y0 + i * AGENTS_ROW_H;
        lv_obj_t *sep;

        ag.rows[i] = lv_obj_create(ag.root);
        cdt_uii_box(ag.rows[i], 8, y, 384, AGENTS_ROW_H);

        ag.row_mark[i] = cdt_uii_text(ag.rows[i], F_BAR, 0, 4, 14, 18);
        lv_obj_set_style_text_align(ag.row_mark[i], LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

        ag.row_state[i] = cdt_uii_text(ag.rows[i], F_BAR, 16, 4, 150, 18);
        lv_label_set_long_mode(ag.row_state[i], LV_LABEL_LONG_DOT);

        /* ZC5：行右让位给右对齐 elapsed（"hh:mm:ss" 最长 9 字符），项目名
         * 列预算同步收窄（CDT_VIEW_AGENTS_PROJECT_MAX_COLS）。 */
        ag.row_project[i] = cdt_uii_text(ag.rows[i], F_BAR, 170, 4, 124, 18);
        lv_label_set_long_mode(ag.row_project[i], LV_LABEL_LONG_DOT);

        ag.row_elapsed[i] = cdt_uii_text(ag.rows[i], F_BAR, 300, 4, 76, 18);
        lv_label_set_long_mode(ag.row_elapsed[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_align(ag.row_elapsed[i], LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

        /* ZC8：第二行活动文本（缩进对齐状态词列；空文本时 apply 隐藏）。 */
        ag.row_activity[i] = cdt_uii_text(ag.rows[i], F_BAR, 16, 24, 284, 18);
        lv_label_set_long_mode(ag.row_activity[i], LV_LABEL_LONG_DOT);
        lv_obj_add_flag(ag.row_activity[i], LV_OBJ_FLAG_HIDDEN);

        if (i > 0) { /* 行分隔线 */
            sep = lv_obj_create(ag.rows[i]);
            cdt_uii_box(sep, 0, -1, 384, 1);
            lv_obj_set_style_bg_color(sep, lv_color_black(), LV_PART_MAIN);
            lv_obj_set_style_bg_opa(sep, LV_OPA_COVER, LV_PART_MAIN);
            lv_obj_set_style_border_width(sep, 0, LV_PART_MAIN);
        }
        lv_obj_add_flag(ag.rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    ag.trunc = cdt_uii_text(ag.root, F_BAR, 8,
                            AGENTS_ROW_Y0 + AGENTS_ROWS * AGENTS_ROW_H + 4,
                            384, 18);
    lv_obj_add_flag(ag.trunc, LV_OBJ_FLAG_HIDDEN);

    /* ZC8：操作提示行（纯文案；短按翻页本来就能到 DETAILS，KEY 行为不变）。 */
    ag.hint = cdt_uii_text(ag.root, F_BAR, 8,
                           AGENTS_ROW_Y0 + AGENTS_ROWS * AGENTS_ROW_H + 26,
                           384, 18);
    lv_label_set_text(ag.hint, "PRESS KEY: DETAILS");

    ag.empty = cdt_uii_text(ag.root, F_BODY, 8, 150, 384, 24);
    lv_obj_set_style_text_align(ag.empty, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_label_set_text(ag.empty, "NO TASKS");

    cdt_uii_bottom_bar(ag.root, &ag.page_ind, &ag.mute);
}

static void apply_row(int i, const cdt_agents_row_t *row)
{
    if (row->waiting) {
        lv_label_set_text(ag.row_mark[i], "!"); /* 等待提醒标记（独立列，不挤占状态词） */
    }
    else {
        lv_label_set_text(ag.row_mark[i], "");
    }
    cdt_uii_set_text(ag.row_state[i], row->state_label);
    cdt_uii_set_text(ag.row_project[i], row->project);
    cdt_uii_set_text(ag.row_elapsed[i], row->elapsed_text); /* ZC5：行尾时长 */

    /* ZC8：第二行活动文本——空串省略（不占位），有值则显示（presenter 已截断） */
    if (row->activity[0] != '\0') {
        cdt_uii_set_text(ag.row_activity[i], row->activity);
        lv_obj_remove_flag(ag.row_activity[i], LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(ag.row_activity[i], LV_OBJ_FLAG_HIDDEN);
    }

    /* needs_you / error 行强调：反白状态词（黑底白字，§6 等待状态醒目） */
    if (row->emphasized) {
        lv_obj_set_style_bg_color(ag.row_state[i], lv_color_black(), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(ag.row_state[i], LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_text_color(ag.row_state[i], lv_color_white(), LV_PART_MAIN);
    }
    else {
        lv_obj_set_style_bg_opa(ag.row_state[i], LV_OPA_TRANSP, LV_PART_MAIN);
        lv_obj_set_style_text_color(ag.row_state[i], lv_color_black(), LV_PART_MAIN);
    }
    /* 行内反白需占满状态词框：设置背景后补 padding 对齐 */
    lv_obj_set_style_pad_all(ag.row_state[i], 1, LV_PART_MAIN);
    lv_obj_remove_flag(ag.rows[i], LV_OBJ_FLAG_HIDDEN);
}

void cdt_ui_agents_apply(const cdt_view_t *view, const cdt_nav_t *nav)
{
    uint8_t start;
    int i;
    char buf[48];

    if (view == NULL || nav == NULL) return;

    cdt_uii_link_banner_apply(ag.banner, ag.banner_label, view);

    /* ZC8：顶栏右侧显示会话总数（"SESSIONS <threads_total>"，总数语义；
     * 底栏 PAGE: AGENTS i/n 保留翻页指示）。空态同样显示（0 也是真值）。 */
    snprintf(buf, sizeof(buf), "SESSIONS %u", (unsigned)view->threads_total);
    cdt_uii_set_text(ag.sub_ind, buf);

    if (view->agents_count == 0) {
        for (i = 0; i < AGENTS_ROWS; i++) {
            lv_obj_add_flag(ag.rows[i], LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(ag.empty, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(ag.trunc, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        uint8_t pages = view->agents_pages > 0 ? view->agents_pages : 1;
        uint8_t sub = nav->agents_page;

        lv_obj_add_flag(ag.empty, LV_OBJ_FLAG_HIDDEN);
        start = (uint8_t)(sub * AGENTS_ROWS);
        for (i = 0; i < AGENTS_ROWS; i++) {
            uint8_t idx = (uint8_t)(start + (uint8_t)i);
            if (idx < view->agents_count) {
                apply_row(i, &view->agents_rows[idx]);
            }
            else {
                lv_obj_add_flag(ag.rows[i], LV_OBJ_FLAG_HIDDEN);
            }
        }

        /* 总数 / 裁剪标记（§6：显示总数/裁剪标记；与顶栏 SESSIONS 并存） */
        if (view->agents_hidden > 0) {
            snprintf(buf, sizeof(buf), "TOTAL %u (+%u MORE)",
                     (unsigned)view->threads_total, (unsigned)view->agents_hidden);
        }
        else {
            snprintf(buf, sizeof(buf), "TOTAL %u", (unsigned)view->threads_total);
        }
        cdt_uii_set_text(ag.trunc, buf);
        lv_obj_remove_flag(ag.trunc, LV_OBJ_FLAG_HIDDEN);
    }

    cdt_uii_page_ind_set(ag.page_ind, "AGENTS", nav->agents_page + 1,
                         view->agents_count > 0 ? view->agents_pages : 1);
    lv_label_set_text(ag.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
