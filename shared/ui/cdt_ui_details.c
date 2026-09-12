/*
 * cdt_ui_details.c — DETAILS 会话详情页（ZC4，契约 v1.2 增补第 6 屏；ZC8 BRANCH 行）
 *
 * 效果图第 6 屏：label: value 明细行展示"选中会话"（与 NOW 页同一选择规则：
 * selected_thread_id → 线程）的 PROJECT / BRANCH（ZC8，v1.2 threads[].branch）
 * / MODEL / STATUS / DURATION / CONTEXT / INPUT TOKENS / OUTPUT TOKENS /
 * CACHED TOKENS。
 * 数值真源全部来自 ViewModel（presenter details builder 已按列预算截断；
 * 缺值统一 "--"，绝不编造）。v1.2 前的旧桥快照不含 model/tokens/branch →
 * 整列 "--"。布局构件与 USAGE 等页一致（cdt_ui_internal：标题栏/链路横幅/底栏）。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define DETAILS_ROW_H 22      /* ZC8：9 行版行距（8×26 放不下，22×9 仍在底栏上方） */
#define DETAILS_ROW_Y0 64
#define DETAILS_ROW_COUNT 9

typedef struct {
    lv_obj_t *root;
    lv_obj_t *banner;
    lv_obj_t *banner_label;
    lv_obj_t *rows[DETAILS_ROW_COUNT];
    lv_obj_t *page_ind;
    lv_obj_t *mute;
} details_widgets_t;

static details_widgets_t dt;

lv_obj_t *cdt_ui_details_root(void)
{
    return dt.root;
}

static void apply_row(int i, const char *text)
{
    cdt_uii_set_text(dt.rows[i], text);
    lv_obj_remove_flag(dt.rows[i], LV_OBJ_FLAG_HIDDEN);
}

void cdt_ui_details_create(void)
{
    int i;

    dt.root = cdt_uii_page_root();
    cdt_uii_title_row(dt.root, "DETAILS", NULL, NULL);
    dt.banner = cdt_uii_link_banner(dt.root, &dt.banner_label);

    for (i = 0; i < DETAILS_ROW_COUNT; i++) {
        dt.rows[i] = cdt_uii_text(dt.root, F_BAR, 8,
                                  DETAILS_ROW_Y0 + i * DETAILS_ROW_H, 384, 20);
        lv_label_set_long_mode(dt.rows[i], LV_LABEL_LONG_DOT);
        lv_obj_add_flag(dt.rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    cdt_uii_bottom_bar(dt.root, &dt.page_ind, &dt.mute);
}

void cdt_ui_details_apply(const cdt_view_t *view)
{
    char buf[96];

    if (view == NULL) return;

    cdt_uii_link_banner_apply(dt.banner, dt.banner_label, view);

    /* label 列固定 14 显示列（ASCII 8×16），值列对齐；值由 presenter 截断。
     * ZC8：BRANCH 行插在 PROJECT 之后（同属仓库身份信息；null → "--"）。 */
    snprintf(buf, sizeof(buf), "PROJECT       %s", view->project);
    apply_row(0, buf);
    snprintf(buf, sizeof(buf), "BRANCH        %s", view->branch_text);
    apply_row(1, buf);
    snprintf(buf, sizeof(buf), "MODEL         %s", view->model_text);
    apply_row(2, buf);
    snprintf(buf, sizeof(buf), "STATUS        %s", view->status_label);
    apply_row(3, buf);
    snprintf(buf, sizeof(buf), "DURATION      %s", view->elapsed_text);
    apply_row(4, buf);
    snprintf(buf, sizeof(buf), "CONTEXT       %s", view->context_detail_text);
    apply_row(5, buf);
    snprintf(buf, sizeof(buf), "INPUT TOKENS  %s", view->tokens_in_text);
    apply_row(6, buf);
    snprintf(buf, sizeof(buf), "OUTPUT TOKENS %s", view->tokens_out_text);
    apply_row(7, buf);
    snprintf(buf, sizeof(buf), "CACHED TOKENS %s", view->tokens_cached_text);
    apply_row(8, buf);

    cdt_uii_page_ind_set(dt.page_ind, "DETAILS", 1, 1);
    lv_label_set_text(dt.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
