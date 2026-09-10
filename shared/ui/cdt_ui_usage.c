/*
 * cdt_ui_usage.c — USAGE 页（P2.2，A2）
 *
 * §6 USAGE 行：实际窗口长度（duration_mins 来自数据，不硬编码 5h/周）、
 * usedPercent、reset 倒计时（presenter 按 resets_at_ms−业务时钟计算；
 * 已过 reset 显示 EXPIRED 不猜 0%）、context（若可得；无百分比 → "--"，
 * 累计 token 不冒充 context）。额度缺失整页显示 "--"（S18）。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define USAGE_ROW_H 30
#define USAGE_ROW_Y0 64

typedef struct {
    lv_obj_t *root;
    lv_obj_t *banner;
    lv_obj_t *banner_label;
    lv_obj_t *rows[CDT_MAX_USAGE_WINDOWS];
    lv_obj_t *empty;     /* 额度缺失 "--" */
    lv_obj_t *context;   /* "CTX 43%" / "CTX --" */
    lv_obj_t *page_ind;
    lv_obj_t *mute;
} usage_widgets_t;

static usage_widgets_t us;

lv_obj_t *cdt_ui_usage_root(void)
{
    return us.root;
}

static void apply_row(int i, const cdt_usage_row_t *row)
{
    char buf[96];
    char rst[24];

    if (row->reset_present) {
        if (row->reset_in_s < 0) {
            snprintf(rst, sizeof(rst), "RST EXPIRED"); /* 已过 reset，不猜 0% */
        }
        else {
            uint32_t s = (uint32_t)row->reset_in_s;
            snprintf(rst, sizeof(rst), "RST %02u:%02u:%02u",
                     (unsigned)(s / 3600u), (unsigned)((s % 3600u) / 60u),
                     (unsigned)(s % 60u));
        }
    }
    else {
        snprintf(rst, sizeof(rst), "RST --");
    }

    if (row->pct_present) {
        snprintf(buf, sizeof(buf), "%s %u%% %um %s",
                 row->label, (unsigned)row->pct, (unsigned)row->duration_mins, rst);
    }
    else {
        snprintf(buf, sizeof(buf), "%s -- %um %s",
                 row->label, (unsigned)row->duration_mins, rst);
    }
    cdt_uii_set_text(us.rows[i], buf);
    lv_obj_remove_flag(us.rows[i], LV_OBJ_FLAG_HIDDEN);
}

void cdt_ui_usage_create(void)
{
    int i;

    us.root = cdt_uii_page_root();
    cdt_uii_title_row(us.root, "USAGE", NULL, NULL);
    us.banner = cdt_uii_link_banner(us.root, &us.banner_label);

    for (i = 0; i < CDT_MAX_USAGE_WINDOWS; i++) {
        us.rows[i] = cdt_uii_text(us.root, F_BAR, 8, USAGE_ROW_Y0 + i * USAGE_ROW_H, 384, 20);
        lv_label_set_long_mode(us.rows[i], LV_LABEL_LONG_DOT);
        lv_obj_add_flag(us.rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    us.empty = cdt_uii_text(us.root, F_BODY, 8, 100, 384, 24);
    lv_obj_set_style_text_align(us.empty, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_label_set_text(us.empty, "USAGE --");

    us.context = cdt_uii_text(us.root, F_BODY, 8, 226, 384, 22);

    cdt_uii_bottom_bar(us.root, &us.page_ind, &us.mute);
}

void cdt_ui_usage_apply(const cdt_view_t *view)
{
    int i;

    if (view == NULL) return;

    cdt_uii_link_banner_apply(us.banner, us.banner_label, view);

    if (view->usage_count == 0) {
        for (i = 0; i < CDT_MAX_USAGE_WINDOWS; i++) {
            lv_obj_add_flag(us.rows[i], LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(us.empty, LV_OBJ_FLAG_HIDDEN); /* 额度缺失 -- */
    }
    else {
        lv_obj_add_flag(us.empty, LV_OBJ_FLAG_HIDDEN);
        for (i = 0; i < CDT_MAX_USAGE_WINDOWS; i++) {
            if (i < (int)view->usage_count) {
                apply_row(i, &view->usage_rows[i]);
            }
            else {
                lv_obj_add_flag(us.rows[i], LV_OBJ_FLAG_HIDDEN);
            }
        }
    }

    cdt_uii_set_text(us.context, view->context_text);
    cdt_uii_page_ind_set(us.page_ind, "USAGE", 1, 1);
    lv_label_set_text(us.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
