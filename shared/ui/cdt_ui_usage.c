/*
 * cdt_ui_usage.c — USAGE 页（P2.2，A2；ZC5 CONTEXT 行；ZC7 图形化窗口块）
 *
 * §6 USAGE 行：实际窗口长度（duration_mins 来自数据，不硬编码 5h/周）、
 * usedPercent、reset 倒计时（presenter 按 resets_at_ms−业务时钟计算；
 * 已过 reset 显示 EXPIRED 不猜 0%）。额度缺失整页显示 "--"（S18）。
 * ZC5：windows 列表下方 CONTEXT 行。
 * ZC7（效果图 5 对齐）：每窗口一组【标签行（"<N> HOUR WINDOW"/"<M> MIN
 * WINDOW"，presenter 由 duration_mins 推导，原始 label 不上屏）+ 20 段图形
 * 分段进度条（已填黑/未填白+1px 边框，单色审美）+ 右侧大号百分比
 * （montserrat_28）+ "RESET IN hh:mm:ss / h:mm" 倒计时行】；下方 4 行
 * token 表（CONTEXT/INPUT/OUTPUT/CACHED，值复用 DETAILS 透传字段，
 * 缺值 "--"）。窗口块最多画 2 组（400x300 布局预算），超出部分标题行右
 * 侧 "+N MORE" 诚实标注（不静默丢弃）。pct null → 条画空 + 右显 "--"。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

#define USAGE_BLOCKS 2                             /* 窗口块上限（布局预算） */
#define USAGE_BLOCK_STRIDE 69                      /* 块步进：18+30+18+3 */
#define USAGE_BLOCK_Y0 56                          /* 链路横幅(36..56)之下 */
#define USAGE_SEG_W 12                             /* 段宽/段高/段间距 */
#define USAGE_SEG_H 14
#define USAGE_SEG_GAP 2
#define USAGE_TABLE_Y0 194                         /* token 表首行（固定位置） */
#define USAGE_TABLE_ROWS 4

typedef struct {
    lv_obj_t *root;
    lv_obj_t *banner;
    lv_obj_t *banner_label;
    lv_obj_t *more;      /* 标题行右侧 "+N MORE"（窗口 >2 组时） */
    lv_obj_t *wlabel[USAGE_BLOCKS];  /* 标签行 "5 HOUR WINDOW" */
    lv_obj_t *seg[USAGE_BLOCKS][CDT_VIEW_USAGE_SEGMENTS]; /* 分段进度条 */
    lv_obj_t *pct[USAGE_BLOCKS];     /* 右侧大号百分比（F_STATUS） */
    lv_obj_t *reset[USAGE_BLOCKS];   /* "RESET IN hh:mm:ss" 倒计时行 */
    lv_obj_t *empty;     /* 额度缺失 "USAGE --" */
    lv_obj_t *tok_label[USAGE_TABLE_ROWS]; /* token 表：CONTEXT/INPUT/… */
    lv_obj_t *tok_value[USAGE_TABLE_ROWS]; /* 值列（复用 DETAILS 透传） */
    lv_obj_t *page_ind;
    lv_obj_t *mute;
} usage_widgets_t;

static usage_widgets_t us;

lv_obj_t *cdt_ui_usage_root(void)
{
    return us.root;
}

static void apply_block(int i, const cdt_usage_row_t *row)
{
    char buf[12];
    int k, filled;

    cdt_uii_set_text(us.wlabel[i], row->title);
    cdt_uii_set_text(us.reset[i], row->reset_text);

    /* pct null → 条画空 + 右显 "--"（不猜百分比） */
    if (row->pct_present) {
        snprintf(buf, sizeof(buf), "%u%%", (unsigned)row->pct);
        cdt_uii_set_text(us.pct[i], buf);
        filled = ((int)row->pct * CDT_VIEW_USAGE_SEGMENTS + 50) / 100;
    }
    else {
        cdt_uii_set_text(us.pct[i], "--");
        filled = 0;
    }

    /* 分段进度条：已填黑 / 未填白+1px 边框（单色审美，效果图 5） */
    for (k = 0; k < CDT_VIEW_USAGE_SEGMENTS; k++) {
        bool on = (k < filled);

        lv_obj_set_style_bg_color(us.seg[i][k],
                                  on ? lv_color_black() : lv_color_white(),
                                  LV_PART_MAIN);
        lv_obj_set_style_bg_opa(us.seg[i][k], LV_OPA_COVER, LV_PART_MAIN);
    }

    lv_obj_remove_flag(us.wlabel[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(us.pct[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(us.reset[i], LV_OBJ_FLAG_HIDDEN);
    for (k = 0; k < CDT_VIEW_USAGE_SEGMENTS; k++) {
        lv_obj_remove_flag(us.seg[i][k], LV_OBJ_FLAG_HIDDEN);
    }
}

static void hide_block(int i)
{
    int k;

    lv_obj_add_flag(us.wlabel[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(us.pct[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(us.reset[i], LV_OBJ_FLAG_HIDDEN);
    for (k = 0; k < CDT_VIEW_USAGE_SEGMENTS; k++) {
        lv_obj_add_flag(us.seg[i][k], LV_OBJ_FLAG_HIDDEN);
    }
}

void cdt_ui_usage_create(void)
{
    static const char *const tok_names[USAGE_TABLE_ROWS] = {
        "CONTEXT", "INPUT TOKENS", "OUTPUT TOKENS", "CACHED TOKENS"
    };
    int i, k;

    us.root = cdt_uii_page_root();
    cdt_uii_title_row(us.root, "USAGE", NULL, &us.more);
    us.banner = cdt_uii_link_banner(us.root, &us.banner_label);

    /* ---- 窗口块（≤2 组）：标签行 / 分段条+大号百分比 / 倒计时行 ---- */
    for (i = 0; i < USAGE_BLOCKS; i++) {
        int y = USAGE_BLOCK_Y0 + i * USAGE_BLOCK_STRIDE;

        us.wlabel[i] = cdt_uii_text(us.root, F_BAR, 8, y, 280, 18);
        lv_label_set_long_mode(us.wlabel[i], LV_LABEL_LONG_DOT);

        for (k = 0; k < CDT_VIEW_USAGE_SEGMENTS; k++) {
            lv_obj_t *s = lv_obj_create(us.root);

            cdt_uii_box(s, 8 + k * (USAGE_SEG_W + USAGE_SEG_GAP), y + 26,
                        USAGE_SEG_W, USAGE_SEG_H);
            lv_obj_set_style_bg_color(s, lv_color_white(), LV_PART_MAIN);
            lv_obj_set_style_bg_opa(s, LV_OPA_COVER, LV_PART_MAIN);
            lv_obj_set_style_border_color(s, lv_color_black(), LV_PART_MAIN);
            lv_obj_set_style_border_width(s, 1, LV_PART_MAIN);
            lv_obj_set_style_radius(s, 0, LV_PART_MAIN);
            lv_obj_add_flag(s, LV_OBJ_FLAG_HIDDEN);
            us.seg[i][k] = s;
        }

        us.pct[i] = cdt_uii_text(us.root, F_STATUS, 290, y + 18, 102, 30);
        lv_obj_set_style_text_align(us.pct[i], LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

        us.reset[i] = cdt_uii_text(us.root, F_BAR, 8, y + 50, 384, 18);
        lv_label_set_long_mode(us.reset[i], LV_LABEL_LONG_DOT);

        lv_obj_add_flag(us.wlabel[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(us.pct[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(us.reset[i], LV_OBJ_FLAG_HIDDEN);
    }

    us.empty = cdt_uii_text(us.root, F_BODY, 8, 100, 384, 24);
    lv_obj_set_style_text_align(us.empty, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_label_set_text(us.empty, "USAGE --");

    /* ---- ZC7：token 表 4 行（效果图 5；值复用 DETAILS 页透传字段，
     * 缺值由 presenter 统一 "--"；位置恒定，与额度有无无关）---- */
    for (i = 0; i < USAGE_TABLE_ROWS; i++) {
        int y = USAGE_TABLE_Y0 + i * 18;

        us.tok_label[i] = cdt_uii_text(us.root, F_BAR, 8, y, 140, 18);
        lv_label_set_text(us.tok_label[i], tok_names[i]);

        us.tok_value[i] = cdt_uii_text(us.root, F_BAR, 150, y, 242, 18);
        lv_label_set_long_mode(us.tok_value[i], LV_LABEL_LONG_DOT);
    }

    cdt_uii_bottom_bar(us.root, &us.page_ind, &us.mute);
}

void cdt_ui_usage_apply(const cdt_view_t *view)
{
    int i;

    if (view == NULL) return;

    cdt_uii_link_banner_apply(us.banner, us.banner_label, view);

    if (view->usage_count == 0) {
        for (i = 0; i < USAGE_BLOCKS; i++) {
            hide_block(i);
        }
        lv_obj_remove_flag(us.empty, LV_OBJ_FLAG_HIDDEN); /* 额度缺失 -- */
    }
    else {
        lv_obj_add_flag(us.empty, LV_OBJ_FLAG_HIDDEN);
        for (i = 0; i < USAGE_BLOCKS; i++) {
            if (i < (int)view->usage_count) {
                apply_block(i, &view->usage_rows[i]);
            }
            else {
                hide_block(i);
            }
        }
    }

    /* 窗口块上限诚实标注：>2 组时标题行右侧 "+N MORE"（不静默丢弃）。
     * A0：顶栏时钟占用右槽（效果图 5 顶栏时间位），MORE 并存。 */
    {
        char buf[16];

        if (view->clock_text[0] != '\0') {
            snprintf(buf, sizeof(buf), "%s", view->clock_text);
        }
        else {
            buf[0] = '\0';
        }
        if (view->usage_count > USAGE_BLOCKS) {
            char more[16];

            snprintf(more, sizeof(more), "+%u MORE",
                     (unsigned)(view->usage_count - USAGE_BLOCKS));
            if (buf[0] != '\0') {
                char merged[24];
                snprintf(merged, sizeof(merged), "%s  %s", buf, more);
                cdt_uii_set_text(us.more, merged);
            }
            else {
                cdt_uii_set_text(us.more, more);
            }
        }
        else {
            cdt_uii_set_text(us.more, buf);
        }
    }

    /* ZC7：token 表（CONTEXT=DETAILS 同源三态；INPUT/OUTPUT/CACHED K 格式） */
    cdt_uii_set_text(us.tok_value[0], view->context_detail_text);
    cdt_uii_set_text(us.tok_value[1], view->tokens_in_text);
    cdt_uii_set_text(us.tok_value[2], view->tokens_out_text);
    cdt_uii_set_text(us.tok_value[3], view->tokens_cached_text);

    cdt_uii_page_ind_set(us.page_ind, "USAGE", 1, 1);
    lv_label_set_text(us.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
