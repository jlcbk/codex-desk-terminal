/*
 * cdt_ui_now.c — NOW 页构建与刷新（P2.1 创建；P2.2 挂入整页根容器，A2；
 *                 ZC5 仪表盘化对齐效果图 1，A5；ZC6 两态精修+专用警报布局，A6）
 *
 * ZC6 布局（400×300，外边距 8px，纯黑白两色，自上而下）——两态：
 *
 * 【常规态】working/idle/done/error/thinking（效果图 1）：
 *   y=  8.. 32  标题栏：项目名（左，dot 截断）+ 电压（右）
 *   y=  36.. 56 链路提示条 20px（fresh 时隐藏；反白黑底白字）
 *   y=  58..102 主状态区 44px：状态词左对齐（error 粗框），左侧实心圆点
 *               "●"（unifont U+25CF，cdt_font_unifont16 已覆盖该码点）
 *   y= 106..124 活动摘要 18px（LV_LABEL_LONG_DOT 像素级省略号兜底）
 *   y= 126..144 "RUNNING FOR mm:ss" 18px（ZC6 新增，时长紧跟活动；
 *               右侧同排保留 CANCELLED 位，§6 取消可见）
 *   y= 148..240 PLAN mini 面板 92px（细边框 1px）：标题行 "CURRENT PLAN"（左）
 *               + 计数 "n / total"（右）；下接最多 4 步行（17px 间距），
 *               每行「ASCII 标记 + 截断文本」（completed="[x]"、in_progress=">"、
 *               pending="o"）；plan.total==0 → 整面板隐藏（不留空框）
 *   y= 244..262 底部信息行 18px（三选一互斥）：
 *               attention 在场 → "N PENDING: …"（优先，警报数据不让位额度）；
 *               否则信息条 左 "CTX 68%"/"CTX 578K" + 右 首窗口短词 "5H 72%"
 *               （ZC6 上移至贴底，原左下 ELAPSED 槽位取消——时长已上移）
 *   y= 266..268 底栏分隔线；y=268..292 底栏 24px：页面指示（左）/静音文字标签（右）
 *
 * 【警报态】needs_you 专用布局（效果图 2，alarm_mode，覆盖常规内容）：
 *   y=  58..102 反白 NEEDS YOU 横幅（黑底白字居中，粗框）
 *   y= 106..124 "PERMISSION REQUIRED" 标签行（居中；多条 pending 追加计数）
 *   y= 128..172 命令独立边框盒（细边框 1px）："$ <attention 摘要（已脱敏）>"
 *               居中；无 attention 时隐藏盒
 *   y= 184..202 "WAITING FOR APPROVAL <等待时长>"（居中）
 *   y= 244..262 提示行 "HOLD KEY = MUTE"（真实键语义：长按=静音，居中）
 *   隐藏：PLAN 面板、CTX/窗口信息条、活动行、RUNNING FOR、attention 常规行
 *   （警报突出，位置让给警报元素）
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

/* "●" U+25CF（unifont 已覆盖，gen_font_unifont 转换器逐点核验过）。
 * 经 lv_label_set_text 直写（不走 cdt_ui_ascii_safe，避免被折为 '?'）。 */
#define NOW_DOT "\xE2\x97\x8F"

typedef struct {
    lv_obj_t *project;      /* 标题栏左：项目名 */
    lv_obj_t *voltage;      /* 标题栏右：电压 */
    lv_obj_t *link_bar;     /* 链路提示条（容器） */
    lv_obj_t *link_label;
    lv_obj_t *status_box;   /* 主状态区（容器，承载反白/粗框） */
    lv_obj_t *status_label;
    lv_obj_t *status_dot;   /* ZC6：状态词左侧实心圆点（常规态） */
    lv_obj_t *activity;
    lv_obj_t *running;      /* ZC6："RUNNING FOR mm:ss"（活动行下一行） */
    lv_obj_t *wait;         /* "CANCELLED"（§6 取消可见；ZC6 移至时长行右侧） */
    lv_obj_t *plan_panel;   /* PLAN mini 面板（细边框容器） */
    lv_obj_t *plan_title;   /* ZC6：面板标题行左 "CURRENT PLAN" */
    lv_obj_t *plan_mark[NOW_PLAN_ROWS];   /* 行首 ASCII 标记 [x] / > / o */
    lv_obj_t *plan_text[NOW_PLAN_ROWS];   /* 步骤截断文本 */
    lv_obj_t *plan_counter; /* 标题行右 "n / total" */
    lv_obj_t *info_ctx;     /* 信息条左 "CTX 68%" / "CTX 578K"（ZC6 贴底） */
    lv_obj_t *info_usage;   /* 信息条右 "5H 72%" */
    lv_obj_t *attention;    /* attention 常规行（与信息条同排互斥） */
    lv_obj_t *perm_label;   /* ZC6 警报态："PERMISSION REQUIRED"（+计数） */
    lv_obj_t *cmd_box;      /* ZC6 警报态：命令独立边框盒 */
    lv_obj_t *cmd_label;
    lv_obj_t *wait_appr;    /* ZC6 警报态："WAITING FOR APPROVAL mm:ss" */
    lv_obj_t *alarm_hint;   /* ZC6 警报态："HOLD KEY = MUTE" */
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

    /* ---- 主状态区 44px：needs_you 反白横幅 / error 粗框；常规态状态词
     * 左对齐（ZC6），警报态居中（apply 切换 text_align/pad）---- */
    w.status_box = lv_obj_create(scr);
    cdt_uii_box(w.status_box, 8, 58, 384, 44);

    w.status_label = cdt_uii_text(w.status_box, F_STATUS, 8, 6, 368, 32);

    /* 指示点（ZC6 效果图 1 "● WORKING"）：unifont 16px，状态区左侧垂直居中；
     * 字形直写不走 ascii_safe。 */
    w.status_dot = cdt_uii_text(w.status_box, F_BAR, 10, 14, 16, 16);
    lv_label_set_text(w.status_dot, NOW_DOT);
    lv_obj_add_flag(w.status_dot, LV_OBJ_FLAG_HIDDEN);

    /* ---- 活动摘要（像素级 dot 截断兜底；警报态隐藏）---- */
    w.activity = cdt_uii_text(scr, F_BODY, 8, 106, 384, 18);
    lv_label_set_long_mode(w.activity, LV_LABEL_LONG_DOT);

    /* ---- RUNNING FOR 行（ZC6，效果图 1：时长紧跟活动）----
     * 左 "RUNNING FOR mm:ss"；右侧同排 CANCELLED 位（§6 取消可见）。 */
    w.running = cdt_uii_text(scr, F_BODY, 8, 126, 240, 18);

    w.wait = cdt_uii_text(scr, F_BODY, 240, 126, 152, 18);
    lv_obj_set_style_text_align(w.wait, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    /* ---- PLAN mini 面板：细边框 1px，标题行 + 最多 4 步 ----
     * 面板坐标 (8,148,384,92)；标题行 rel y=2："CURRENT PLAN"（左）+
     * 计数 "n / total"（右对齐）；步骤行 rel y=22+i*17：标记 x=2、文本 x=32
     * （dot 兜底）。total==0 时整个面板隐藏（apply）。*/
    w.plan_panel = lv_obj_create(scr);
    cdt_uii_box(w.plan_panel, 8, 148, 384, 92);
    lv_obj_set_style_border_color(w.plan_panel, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_border_width(w.plan_panel, 1, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.plan_panel, LV_OPA_TRANSP, LV_PART_MAIN);

    w.plan_title = cdt_uii_text(w.plan_panel, F_BAR, 4, 2, 200, 18);
    lv_label_set_text(w.plan_title, "CURRENT PLAN");

    w.plan_counter = cdt_uii_text(w.plan_panel, F_BAR, 272, 2, 108, 18);
    lv_obj_set_style_text_align(w.plan_counter, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

    for (i = 0; i < NOW_PLAN_ROWS; i++) {
        w.plan_mark[i] = cdt_uii_text(w.plan_panel, F_BAR, 2, 22 + i * 17, 28, 18);
        w.plan_text[i] = cdt_uii_text(w.plan_panel, F_BAR, 32, 22 + i * 17, 348, 18);
        lv_label_set_long_mode(w.plan_text[i], LV_LABEL_LONG_DOT);
        lv_obj_add_flag(w.plan_mark[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.plan_text[i], LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_add_flag(w.plan_panel, LV_OBJ_FLAG_HIDDEN);

    /* ---- 警报态元素（ZC6，效果图 2；默认隐藏，alarm_mode 时显示）---- */
    w.perm_label = cdt_uii_text(scr, F_BAR, 8, 106, 384, 18);
    lv_obj_set_style_text_align(w.perm_label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_add_flag(w.perm_label, LV_OBJ_FLAG_HIDDEN);

    w.cmd_box = lv_obj_create(scr);
    cdt_uii_box(w.cmd_box, 8, 128, 384, 44);
    lv_obj_set_style_border_color(w.cmd_box, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_border_width(w.cmd_box, 1, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(w.cmd_box, LV_OPA_TRANSP, LV_PART_MAIN);

    w.cmd_label = cdt_uii_label(w.cmd_box, F_BAR);
    lv_obj_center(w.cmd_label);
    lv_obj_add_flag(w.cmd_box, LV_OBJ_FLAG_HIDDEN);

    w.wait_appr = cdt_uii_text(scr, F_BAR, 8, 184, 384, 18);
    lv_obj_set_style_text_align(w.wait_appr, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_add_flag(w.wait_appr, LV_OBJ_FLAG_HIDDEN);

    w.alarm_hint = cdt_uii_text(scr, F_BAR, 8, 244, 384, 18);
    lv_obj_set_style_text_align(w.alarm_hint, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_add_flag(w.alarm_hint, LV_OBJ_FLAG_HIDDEN);

    /* ---- 底部信息行 y=244（ZC6 贴底，效果图 1；三选一互斥，见 apply）----
     * attention 常规行 / 信息条（左 CTX + 右首窗口短词）/ 警报提示行。 */
    w.attention = cdt_uii_text(scr, F_BODY, 8, 244, 384, 18);
    lv_label_set_long_mode(w.attention, LV_LABEL_LONG_DOT);

    w.info_ctx = cdt_uii_text(scr, F_BODY, 8, 244, 184, 18);

    w.info_usage = cdt_uii_text(scr, F_BODY, 208, 244, 184, 18);
    lv_obj_set_style_text_align(w.info_usage, LV_TEXT_ALIGN_RIGHT, LV_PART_MAIN);

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

    /* ---- ZC6 两态切换：状态词对齐 + 指示点 ----
     * 警报态：横幅文字居中、无点；常规态：左对齐（效果图 1 "● WORKING"），
     * 有指示点时文字左移出点位（pad_left 腾出 unifont 16px 点宽 + 间距）。 */
    if (view->alarm_mode) {
        lv_obj_set_style_text_align(w.status_label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
        lv_obj_set_style_pad_left(w.status_label, 0, LV_PART_MAIN);
        lv_obj_add_flag(w.status_dot, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_set_style_text_align(w.status_label, LV_TEXT_ALIGN_LEFT, LV_PART_MAIN);
        lv_obj_set_style_pad_left(w.status_label, view->status_dot ? 26 : 0, LV_PART_MAIN);
        if (view->status_dot) lv_obj_remove_flag(w.status_dot, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(w.status_dot, LV_OBJ_FLAG_HIDDEN);
    }

    /* ---- 常规态内容行（警报态整体隐藏，位置让给警报元素）---- */
    if (view->alarm_mode) {
        lv_obj_add_flag(w.activity, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.running, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        set_text_ascii(w.activity, view->activity);
        lv_obj_remove_flag(w.activity, LV_OBJ_FLAG_HIDDEN);

        /* ZC6：时长紧跟活动（效果图 1 "Running for 02:41"），原底部 ELAPSED
         * 槽位取消；无任务/无快照（"--"）整行隐藏。
         * A0：标签按状态区分——working/thinking="RUNNING FOR"（进行中），
         * 其余（done/idle/error）时长已定格 → "LAST RUN"（上一轮时长）。 */
        if (view->elapsed_present) {
            snprintf(buf, sizeof(buf), "%s %s",
                     view->elapsed_running ? "RUNNING FOR" : "LAST RUN",
                     view->elapsed_text);
            set_text_ascii(w.running, buf);
            lv_obj_remove_flag(w.running, LV_OBJ_FLAG_HIDDEN);
        }
        else {
            lv_obj_add_flag(w.running, LV_OBJ_FLAG_HIDDEN);
        }
    }

    /* CANCELLED 位（§6 取消可见；WAITING 已随 ZC6 移入警报等待行） */
    if (view->cancelled) {
        lv_label_set_text(w.wait, "CANCELLED");
    }
    else {
        lv_label_set_text(w.wait, "");
    }

    /* ---- PLAN mini 面板：total==0 → 整体隐藏（无 plan 不留空框）；
     * 警报态隐藏（ZC6，警报突出）----
     * 取 plan_steps 前 4 条（原始顺序）；计数 = completed / total。 */
    if (view->plan_present && !view->alarm_mode) {
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

    /* ---- 底部信息行 y=244（三选一互斥）+ 警报元素（ZC6）---- */
    if (view->alarm_mode) {
        /* 警报态：PERMISSION REQUIRED（多条 pending 显示计数）+ 命令边框盒
         * （attention 摘要已脱敏，加 shell 提示前缀）+ 等待审批 + 静音提示；
         * 常规信息行/信息条全部隐藏。 */
        if (view->pending_count > 1) {
            snprintf(buf, sizeof(buf), "PERMISSION REQUIRED (%u PENDING)",
                     (unsigned)view->pending_count);
            set_text_ascii(w.perm_label, buf);
        }
        else {
            lv_label_set_text(w.perm_label, "PERMISSION REQUIRED");
        }
        lv_obj_remove_flag(w.perm_label, LV_OBJ_FLAG_HIDDEN);

        if (view->attention_present) {
            snprintf(buf, sizeof(buf), "$ %s", view->attention);
            cdt_uii_set_text(w.cmd_label, buf);
            lv_obj_remove_flag(w.cmd_box, LV_OBJ_FLAG_HIDDEN);
        }
        else {
            lv_obj_add_flag(w.cmd_box, LV_OBJ_FLAG_HIDDEN);
        }

        snprintf(buf, sizeof(buf), "WAITING FOR APPROVAL %s", view->waiting_text);
        set_text_ascii(w.wait_appr, buf);
        lv_obj_remove_flag(w.wait_appr, LV_OBJ_FLAG_HIDDEN);

        /* 真实键语义（KEY 冻结：needs_you 短按被拒、长按=静音），不写
         * acknowledge 以免误导（ACK 只表示本地静音，绝不等于批准操作）。 */
        lv_label_set_text(w.alarm_hint, "HOLD KEY = MUTE");
        lv_obj_remove_flag(w.alarm_hint, LV_OBJ_FLAG_HIDDEN);

        lv_obj_add_flag(w.attention, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.info_ctx, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.info_usage, LV_OBJ_FLAG_HIDDEN);
    }
    else {
        lv_obj_add_flag(w.perm_label, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.cmd_box, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.wait_appr, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(w.alarm_hint, LV_OBJ_FLAG_HIDDEN);

        /* 常规态：attention 在场优先占用底部行（警报数据不让位额度信息），
         * 否则显示信息条（空串段自然不可见）。 */
        if (view->attention_present) {
            snprintf(buf, sizeof(buf), "%u PENDING: %s",
                     (unsigned)view->pending_count, view->attention);
            set_text_ascii(w.attention, buf);
            lv_obj_remove_flag(w.attention, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(w.info_ctx, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(w.info_usage, LV_OBJ_FLAG_HIDDEN);
        }
        else {
            lv_obj_add_flag(w.attention, LV_OBJ_FLAG_HIDDEN);
            set_text_ascii(w.info_ctx, view->now_ctx_text);
            set_text_ascii(w.info_usage, view->now_usage_text);
            lv_obj_remove_flag(w.info_ctx, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(w.info_usage, LV_OBJ_FLAG_HIDDEN);
        }
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
