/*
 * cdt_ui_now.c — NOW 页构建与刷新（P2.1 创建；P2.2 挂入整页根容器，A2；
 *                 ZC5 仪表盘化对齐效果图 1，A5）
 *
 * ZC5 布局（400×300，外边距 8px，纯黑白两色，自上而下）：
 *   y=  8.. 32  标题栏：项目名（左，dot 截断）+ 电压（右）
 *   y=  36.. 56 链路提示条 20px（fresh 时隐藏；反白黑底白字）
 *   y=  58..102 主状态区 44px：28px 等宽醒目状态词；
 *               needs_you → 反白（黑底白字）；error → 黑白粗框（3px 黑边框）
 *   y= 106..124 活动摘要 18px（LV_LABEL_LONG_DOT 像素级省略号兜底）
 *   y= 128..204 PLAN mini 面板 76px（ZC5 新增）：细边框（1px），最多 4 步，
 *               每行「ASCII 标记 + 截断文本」（completed="[x]"、in_progress=">"、
 *               pending="o"；unifont 有 ✓ 但与全页 ASCII 风格不一致，暂用 ASCII），
 *               面板右上角计数 "n / total"；plan.total==0 → 整面板隐藏（不留空框）
 *   y= 206..224 信息条 18px（ZC5 新增）：左 = "CTX 68%"/"CTX 578K"，右 =
 *               首窗口短词 "5H 72%"；某段空串即隐藏，两段全无 → 整行隐藏
 *   y= 228..246 运行时长（左）/ 等待时长或 CANCELLED（右）；
 *               WAITING 仅 needs_you 显示（ZC5 语义修复：working 不再显示）
 *   y= 248..266 attention 摘要 18px（无则隐藏）
 *   y= 266..268 底栏分隔线；y=268..292 底栏 24px：页面指示（左）/静音文字标签（右）
 *
 * 历史注记：
 *   P2.2 变更仅为结构：widgets 挂到整页根容器（400×300、透明、坐标不变），
 *   由 cdt_ui.c 按生效页切换可见性。
 *   P2.4 修复（A6）：make_label→make_box 顺序缺陷——全部文本改用顺序安全的
 *   cdt_uii_text（先 remove_style_all/定位，后设字体颜色）；状态词 28px 不受
 *   影响。中文经 cdt_ui_ascii_safe 放行字体已覆盖码点，未覆盖码点折为 '?'。
 * UI 不做任何 IO/网络/ADC 调用。
 */
#include <stdio.h>

#include "cdt_ui.h"
#include "cdt_ui_internal.h"
#include "cdt_ui_now.h"

/* PLAN mini 面板行数上限（效果图 1；与 PLAN 页 4 行/页同为 4） */
#define NOW_PLAN_ROWS 4

static lv_obj_t *now_root;

typedef struct {
    lv_obj_t *project;      /* 标题栏左：项目名 */
    lv_obj_t *voltage;      /* 标题栏右：电压 */
    lv_obj_t *link_bar;     /* 链路提示条（容器） */
    lv_obj_t *link_label;
    lv_obj_t *status_box;   /* 主状态区（容器，承载反白/粗框） */
    lv_obj_t *status_label;
    lv_obj_t *activity;
    lv_obj_t *plan_panel;   /* ZC5：PLAN mini 面板（细边框容器） */
    lv_obj_t *plan_mark[NOW_PLAN_ROWS];   /* 行首 ASCII 标记 [x] / > / o */
    lv_obj_t *plan_text[NOW_PLAN_ROWS];   /* 步骤截断文本 */
    lv_obj_t *plan_counter; /* 面板右上角 "n / total" */
    lv_obj_t *info_ctx;     /* ZC5：信息条左 "CTX 68%" / "CTX 578K" */
    lv_obj_t *info_usage;   /* ZC5：信息条右 "5H 72%" */
    lv_obj_t *elapsed;      /* "ELAPSED 02:00" */
    lv_obj_t *wait;         /* "WAITING 00:05"（仅 needs_you）/ "CANCELLED" */
    lv_obj_t *attention;
    lv_obj_t *page_ind;     /* 底栏左 */
    lv_obj_t *mute;         /* 底栏右 */
} now_widgets_t;

static now_widgets_t w;

lv_obj_t *cdt_ui_now_root(void)
{
    return now_root;
}

static void set_text_ascii(lv_obj_t *label, const char *view_text)
{
    cdt_uii_set_text(label, view_text);
}

void cdt_ui_now_create(void)
{
    lv_obj_t *scr = now_root = cdt_uii_page_root();
    int i;

    /* ---- 标题栏：项目名（左，dot 截断）+ 电压（右）----
     * 全部文本走 cdt_uii_text（remove_style_all 后设字体，顺序安全）。*/
    w.project = cdt_uii_text(scr, F_TITLE, 8, 12, 292, 20);
    lv_label_set_long_mode(w.project, LV_LABEL_LONG_DOT);

    w.voltage = cdt_uii_text(scr, F_TITLE, 292, 12, 100, 20);
    lv_obj_set_style_text_align(w.voltage, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- 链路提示条（反白，fresh 时隐藏；固定位置不回流）---- */
    w.link_bar = lv_obj_create(scr);
    cdt_uii_box(w.link_bar, 8, 36, 384, 20);
    lv_obj_set_style_bg_color(w.link_bar, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.link_bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(w.link_bar, 0, LV_PART_MAIN);

    /* label 自身不再过 make_box，cdt_uii_label 的字体样式保留（14px） */
    w.link_label = cdt_uii_label(w.link_bar, F_BAR);
    lv_obj_set_style_text_color(w.link_label, lv_color_white(), LV_PART_MAIN);
    lv_obj_center(w.link_label);

    /* ---- 主状态区 44px：needs_you 反白 / error 粗框 ---- */
    w.status_box = lv_obj_create(scr);
    cdt_uii_box(w.status_box, 8, 58, 384, 44);

    w.status_label = cdt_uii_label(w.status_box, F_STATUS);
    lv_obj_center(w.status_label);

    /* ---- 活动摘要（像素级 dot 截断兜底）---- */
    w.activity = cdt_uii_text(scr, F_BODY, 8, 106, 384, 18);
    lv_label_set_long_mode(w.activity, LV_LABEL_LONG_DOT);

    /* ---- PLAN mini 面板（ZC5，效果图 1）：细边框 1px、最多 4 步 ----
     * 面板坐标 (8,128,384,76)；行内相对坐标：标记 x=2、文本 x=32（dot 兜底）、
     * 计数右上角右对齐。total==0 时整个面板隐藏（apply）。*/
    w.plan_panel = lv_obj_create(scr);
    cdt_uii_box(w.plan_panel, 8, 128, 384, 76);
    lv_obj_set_style_border_color(w.plan_panel, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_border_width(w.plan_panel, 1, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.plan_panel, LV_OPA_TRANSP, LV_PART_MAIN);

    for (i = 0; i < NOW_PLAN_ROWS; i++) {
        w.plan_mark[i] = cdt_uii_text(w.plan_panel, F_BAR, 2, 4 + i * 18, 28, 18);
        w.plan_text[i] = cdt_uii_text(w.plan_panel, F_BAR, 32, 4 + i * 18, 236, 18);
        lv_label_set_long_mode(w.plan_text[i], LV_LABEL_LONG_DOT);
        lv_obj_add_flag(w.plan_mark[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.plan_text[i], LV_OBJ_FLAG_HIDDEN);
    }

    w.plan_counter = cdt_uii_text(w.plan_panel, F_BAR, 272, 4, 108, 18);
    lv_obj_set_style_text_align(w.plan_counter, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    lv_obj_add_flag(w.plan_panel, LV_OBJ_FLAG_HIDDEN);

    /* ---- 信息条（ZC5，效果图 1）：左 CTX / 右首窗口短词（右对齐）---- */
    w.info_ctx = cdt_uii_text(scr, F_BODY, 8, 206, 184, 18);

    w.info_usage = cdt_uii_text(scr, F_BODY, 208, 206, 184, 18);
    lv_obj_set_style_text_align(w.info_usage, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- 运行 / 等待时长 ---- */
    w.elapsed = cdt_uii_text(scr, F_BODY, 8, 228, 240, 18);

    w.wait = cdt_uii_text(scr, F_BODY, 192, 228, 200, 18);
    lv_obj_set_style_text_align(w.wait, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- attention ---- */
    w.attention = cdt_uii_text(scr, F_BODY, 8, 248, 384, 18);
    lv_label_set_long_mode(w.attention, LV_LABEL_LONG_DOT);

    /* ---- 底栏：分隔线 + 页面指示 + 静音文字标签（图标为文字占位）---- */
    {
        lv_obj_t *rule = lv_obj_create(scr);
        cdt_uii_box(rule, 8, 266, 384, 2);
        lv_obj_set_style_bg_color(rule, lv_color_black(), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(rule, LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_border_width(rule, 0, LV_PART_MAIN);
    }
    w.page_ind = cdt_uii_text(scr, F_BAR, 8, 271, 200, 18);

    w.mute = cdt_uii_text(scr, F_BAR, 242, 271, 150, 18);
    lv_obj_set_style_text_align(w.mute, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);
}

static void apply_status_style(const cdt_view_t *view)
{
    bool inverse; /* 反白：黑底白字 */
    bool thick;   /* 粗框：3px 黑边框 */

    if (!view->status_emphasized) {
        inverse = false;
        thick = false;
    }
    else if (view->status == CDT_THREAD_STATE_ERROR) {
        inverse = false;
        thick = true;
    }
    else {
        /* NEEDS YOU 与 LOW BATTERY 强制页：反白强调（§6 等待状态黑白粗框/反白） */
        inverse = true;
        thick = true;
    }

    lv_obj_set_style_bg_color(w.status_box, inverse ? lv_color_black() : lv_color_white(),
                              LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.status_box, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_color(w.status_box, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_border_width(w.status_box, thick ? 3 : 0, LV_PART_MAIN);
    lv_obj_set_style_text_color(w.status_label,
                                inverse ? lv_color_white() : lv_color_black(), LV_PART_MAIN);
}

void cdt_ui_now_apply(const cdt_view_t *view)
{
    char buf[CDT_VIEW_USAGE_BYTES + 24];

    if (view == NULL) return;

    set_text_ascii(w.project, view->project);
    set_text_ascii(w.voltage, view->voltage_text);

    /* 链路提示位（独立于业务状态）：disconnected 提示强度 > stale > source stale */
    if (view->link_disconnected) {
        lv_label_set_text(w.link_label, "LINK DISCONNECTED - TIME FROZEN");
        lv_obj_remove_flag(w.link_bar, LV_OBJ_FLAG_HIDDEN);
    }
    else if (view->link_stale) {
        lv_label_set_text(w.link_label, "LINK STALE - TIME FROZEN");
        lv_obj_remove_flag(w.link_bar, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(w.link_bar, LV_OBJ_FLAG_HIDDEN);
    }

    set_text_ascii(w.status_label, view->status_label);
    apply_status_style(view);

    set_text_ascii(w.activity, view->activity);

    snprintf(buf, sizeof(buf), "ELAPSED %s", view->elapsed_text);
    set_text_ascii(w.elapsed, buf);

    /* ZC5 WAITING 语义：仅 needs_you 显示；CANCELLED 保留（§6 取消可见）；
     * 其他状态该位空白（修复 working 也显示 WAITING 的瑕疵）。 */
    if (view->cancelled) {
        lv_label_set_text(w.wait, "CANCELLED");
    }
    else if (view->waiting_present) {
        snprintf(buf, sizeof(buf), "WAITING %s", view->waiting_text);
        set_text_ascii(w.wait, buf);
    }
    else {
        lv_label_set_text(w.wait, "");
    }

    /* ---- PLAN mini 面板（ZC5）：total==0 → 整体隐藏（无 plan 不留空框）----
     * 取 plan_steps 前 4 条（原始顺序）；计数 = completed / total。 */
    if (view->plan_present) {
        int i;

        snprintf(buf, sizeof(buf), "%u / %u",
                 (unsigned)view->plan_completed, (unsigned)view->plan_total);
        cdt_uii_set_text(w.plan_counter, buf);

        for (i = 0; i < NOW_PLAN_ROWS; i++) {
            if (i < (int)view->plan_step_count) {
                const cdt_plan_step_row_t *row = &view->plan_steps[i];

                switch ((cdt_step_status_t)row->status) {
                    case CDT_STEP_STATUS_COMPLETED:
                        lv_label_set_text(w.plan_mark[i], "[x]");
                        break;
                    case CDT_STEP_STATUS_IN_PROGRESS:
                        lv_label_set_text(w.plan_mark[i], ">"); /* 当前步骤标记 */
                        break;
                    default:
                        lv_label_set_text(w.plan_mark[i], "o");
                        break;
                }
                cdt_uii_set_text(w.plan_text[i], row->text);
                lv_obj_remove_flag(w.plan_mark[i], LV_OBJ_FLAG_HIDDEN);
                lv_obj_remove_flag(w.plan_text[i], LV_OBJ_FLAG_HIDDEN);
            }
            else {
                lv_obj_add_flag(w.plan_mark[i], LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(w.plan_text[i], LV_OBJ_FLAG_HIDDEN);
            }
        }
        lv_obj_remove_flag(w.plan_panel, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(w.plan_panel, LV_OBJ_FLAG_HIDDEN);
    }

    /* ---- 信息条（ZC5）：空串段自然不可见，两段全无 → 整行空白 ---- */
    set_text_ascii(w.info_ctx, view->now_ctx_text);
    set_text_ascii(w.info_usage, view->now_usage_text);

    if (view->attention_present) {
        snprintf(buf, sizeof(buf), "%u PENDING: %s",
                 (unsigned)view->pending_count, view->attention);
        set_text_ascii(w.attention, buf);
        lv_obj_remove_flag(w.attention, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(w.attention, LV_OBJ_FLAG_HIDDEN);
    }

    {
        const char *page_name = "NOW";
        if (view->page == CDT_PAGE_AGENTS) page_name = "AGENTS";
        else if (view->page == CDT_PAGE_PLAN) page_name = "PLAN";
        else if (view->page == CDT_PAGE_USAGE) page_name = "USAGE";
        else if (view->page == CDT_PAGE_LOW_BATTERY) page_name = "LOW BATTERY";
        snprintf(buf, sizeof(buf), "PAGE: %s", page_name);
        set_text_ascii(w.page_ind, buf);
    }

    /* 静音位文字标签（图标为占位文字；ACK 只表示本地静音/已读）*/
    lv_label_set_text(w.mute, view->muted ? "[x] MUTED" : "[ ] SOUND ON");
}
