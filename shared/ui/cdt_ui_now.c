/*
 * cdt_ui_now.c — NOW 页构建与刷新（P2.1 创建；P2.2 挂入整页根容器，A2）
 *
 * §6 NOW 行内容项 → 布局（400×300，外边距 8px，纯黑白两色）：
 *   y=  8.. 36  标题栏 28px：项目名（左）+ 电压（右）
 *   y=  36.. 56 链路提示条 20px（fresh 时隐藏；反白黑底白字）
 *   y=  64..116 主状态区 52px：28px 等宽醒目状态词；
 *               needs_you → 反白（黑底白字）；error → 黑白粗框（3px 黑边框）
 *   y= 124..146 活动摘要 16px（LV_LABEL_LONG_DOT 像素级省略号兜底）
 *   y= 152..172 运行时长（左）/ 等待时长或 CANCELLED（右）
 *   y= 178..198 计划摘要 "PLAN 1/3"
 *   y= 204..224 额度摘要
 *   y= 230..250 attention 摘要（无则隐藏）
 *   y= 266..268 底栏分隔线；y=268..292 底栏 24px：页面指示（左）/静音文字标签（右）
 *
 * P2.2 变更仅为结构：widgets 挂到整页根容器（400×300、透明、坐标不变），
 * 由 cdt_ui.c 按生效页切换可见性；文本/样式/坐标与 P2.1 完全一致（渲染
 * 逐像素不变，见 artifacts/ui 回归对照）。
 *
 * 长文本两道防线：presenter 已按列预算截断（码点安全）+ LVGL dot 模式像素截断。
 * 非 ASCII 字符经 cdt_ui_ascii_safe 显示为 '?'（内置 Montserrat 为 ASCII 字体）。
 * UI 不做任何 IO/网络/ADC 调用。
 */
#include <stdio.h>

#include "cdt_ui.h"
#include "cdt_ui_internal.h"
#include "cdt_ui_now.h"

/* 字体（内置 Montserrat，ASCII；本任务在 lv_conf.h 打开 14/16/28）*/
#define F_TITLE  (&lv_font_montserrat_16)
#define F_STATUS (&lv_font_montserrat_28)
#define F_BODY   (&lv_font_montserrat_16)
#define F_BAR    (&lv_font_montserrat_14)

static lv_obj_t *now_root;

typedef struct {
    lv_obj_t *project;      /* 标题栏左：项目名 */
    lv_obj_t *voltage;      /* 标题栏右：电压 */
    lv_obj_t *link_bar;     /* 链路提示条（容器） */
    lv_obj_t *link_label;
    lv_obj_t *status_box;   /* 主状态区（容器，承载反白/粗框） */
    lv_obj_t *status_label;
    lv_obj_t *activity;
    lv_obj_t *elapsed;      /* "ELAPSED 02:00" */
    lv_obj_t *wait;         /* "WAITING 00:05" / "CANCELLED" */
    lv_obj_t *plan;
    lv_obj_t *usage;
    lv_obj_t *attention;
    lv_obj_t *page_ind;     /* 底栏左 */
    lv_obj_t *mute;         /* 底栏右 */
} now_widgets_t;

static now_widgets_t w;

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font)
{
    return cdt_uii_label(parent, font);
}

static void make_box(lv_obj_t *o, int x, int y, int wd, int ht)
{
    cdt_uii_box(o, x, y, wd, ht);
}

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

    /* ---- 标题栏 28px：项目名（左，dot 截断）+ 电压（右）---- */
    w.project = make_label(scr, F_TITLE);
    make_box(w.project, 8, 12, 292, 20);
    lv_label_set_long_mode(w.project, LV_LABEL_LONG_DOT);
    lv_obj_set_width(w.project, 292);

    w.voltage = make_label(scr, F_TITLE);
    make_box(w.voltage, 292, 12, 100, 20);
    lv_obj_set_style_text_align(w.voltage, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- 链路提示条（反白，fresh 时隐藏；固定位置不回流）---- */
    w.link_bar = lv_obj_create(scr);
    make_box(w.link_bar, 8, 36, 384, 20);
    lv_obj_set_style_bg_color(w.link_bar, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.link_bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(w.link_bar, 0, LV_PART_MAIN);

    w.link_label = make_label(w.link_bar, F_BAR);
    lv_obj_set_style_text_color(w.link_label, lv_color_white(), LV_PART_MAIN);
    lv_obj_center(w.link_label);

    /* ---- 主状态区 52px：needs_you 反白 / error 粗框 ---- */
    w.status_box = lv_obj_create(scr);
    make_box(w.status_box, 8, 64, 384, 52);

    w.status_label = make_label(w.status_box, F_STATUS);
    lv_obj_center(w.status_label);

    /* ---- 活动摘要（像素级 dot 截断兜底）---- */
    w.activity = make_label(scr, F_BODY);
    make_box(w.activity, 8, 124, 384, 20);
    lv_label_set_long_mode(w.activity, LV_LABEL_LONG_DOT);
    lv_obj_set_width(w.activity, 384);

    /* ---- 运行 / 等待时长 ---- */
    w.elapsed = make_label(scr, F_BODY);
    make_box(w.elapsed, 8, 152, 240, 20);

    w.wait = make_label(scr, F_BODY);
    make_box(w.wait, 192, 152, 200, 20);
    lv_obj_set_style_text_align(w.wait, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- 计划摘要 / 额度摘要 / attention ---- */
    w.plan = make_label(scr, F_BODY);
    make_box(w.plan, 8, 178, 384, 20);

    w.usage = make_label(scr, F_BODY);
    make_box(w.usage, 8, 204, 384, 20);

    w.attention = make_label(scr, F_BODY);
    make_box(w.attention, 8, 230, 384, 20);
    lv_label_set_long_mode(w.attention, LV_LABEL_LONG_DOT);
    lv_obj_set_width(w.attention, 384);

    /* ---- 底栏：分隔线 + 页面指示 + 静音文字标签（图标为文字占位）---- */
    {
        lv_obj_t *rule = lv_obj_create(scr);
        make_box(rule, 8, 266, 384, 2);
        lv_obj_set_style_bg_color(rule, lv_color_black(), LV_PART_MAIN);
        lv_obj_set_style_bg_opa(rule, LV_OPA_COVER, LV_PART_MAIN);
        lv_obj_set_style_border_width(rule, 0, LV_PART_MAIN);
    }
    w.page_ind = make_label(scr, F_BAR);
    make_box(w.page_ind, 8, 271, 200, 18);

    w.mute = make_label(scr, F_BAR);
    make_box(w.mute, 242, 271, 150, 18);
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

    if (view->cancelled) {
        /* §6：取消显示 IDLE + 已取消 */
        lv_label_set_text(w.wait, "CANCELLED");
    }
    else {
        snprintf(buf, sizeof(buf), "WAITING %s", view->waiting_text);
        set_text_ascii(w.wait, buf);
    }

    if (view->plan_present) {
        set_text_ascii(w.plan, view->plan_text);
        lv_obj_remove_flag(w.plan, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(w.plan, LV_OBJ_FLAG_HIDDEN);
    }

    set_text_ascii(w.usage, view->usage_text);

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
