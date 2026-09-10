/*
 * cdt_ui_lowbat.c — LOW BATTERY 强制页（P2.3，A2）
 *
 * §6 LOW BATTERY 行全文：电压、设备可用电量、低压提示、充电/唤醒说明；
 * 保护优先（INTERFACES §4：电池故障/CRITICAL/休眠准备强制页 > 普通业务页），
 * 不提供任何退出本页的 UI 手段（短按被 cdt_nav 拒绝，长按只静音不解除）。
 * 文案边界：USB 自动唤醒未证实（docs/HARDWARE.md）→ 明确告知唤醒需按 PWR
 * 重新上电，不承诺"插线即恢复"；不显示编造的精确 SOC（百分比为线性估算）。
 */
#include <stdio.h>

#include "cdt_ui_internal.h"
#include "cdt_ui_pages.h"

typedef struct {
    lv_obj_t *root;
    lv_obj_t *title_box;   /* 反白 LOW BATTERY 大字 */
    lv_obj_t *title_label;
    lv_obj_t *voltage;     /* 大字电压 */
    lv_obj_t *usable;      /* "USABLE 0% (EST)" */
    lv_obj_t *hint[4];     /* 提示/说明行 */
    lv_obj_t *page_ind;
} lowbat_widgets_t;

static lowbat_widgets_t lb;

lv_obj_t *cdt_ui_lowbat_root(void)
{
    return lb.root;
}

void cdt_ui_lowbat_create(void)
{
    int i;

    lb.root = cdt_uii_page_root();

    lb.title_box = lv_obj_create(lb.root);
    cdt_uii_box(lb.title_box, 48, 26, 304, 56);
    lv_obj_set_style_bg_color(lb.title_box, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(lb.title_box, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(lb.title_box, 3, LV_PART_MAIN);
    lv_obj_set_style_border_color(lb.title_box, lv_color_black(), LV_PART_MAIN);

    lb.title_label = cdt_uii_label(lb.title_box, F_STATUS);
    lv_obj_set_style_text_color(lb.title_label, lv_color_white(), LV_PART_MAIN);
    lv_label_set_text(lb.title_label, "LOW BATTERY");
    lv_obj_center(lb.title_label);

    lb.voltage = cdt_uii_text(lb.root, F_STATUS, 8, 94, 384, 34);
    lv_obj_set_style_text_align(lb.voltage, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    lb.usable = cdt_uii_text(lb.root, F_BODY, 8, 130, 384, 22);
    lv_obj_set_style_text_align(lb.usable, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);

    {
        const char *lines[4] = {
            "CONNECT CHARGER NOW",
            "DEVICE WILL SLEEP TO PROTECT BATTERY",
            "TO RESTART AFTER SLEEP: PRESS PWR",
            "(USB AUTO-WAKE UNVERIFIED)"
        };
        for (i = 0; i < 4; i++) {
            lb.hint[i] = cdt_uii_text(lb.root, F_BAR, 8, 168 + i * 22, 384, 18);
            lv_obj_set_style_text_align(lb.hint[i], LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
            lv_label_set_text(lb.hint[i], lines[i]);
        }
    }

    lb.page_ind = cdt_uii_text(lb.root, F_BAR, 8, 271, 200, 18);
    lv_label_set_text(lb.page_ind, "PAGE: LOW BATTERY");
}

void cdt_ui_lowbat_apply(const cdt_view_t *view)
{
    char buf[40];

    if (view == NULL) return;

    /* 电压优先显示（§7.1）；无效 → "--"（不猜值） */
    cdt_uii_set_text(lb.voltage, view->voltage_text);

    /* 可用电量为线性估算（§7.1），标注 EST；无效电池不显示百分比 */
    if (view->battery_valid) {
        snprintf(buf, sizeof(buf), "USABLE %u%% (EST)", (unsigned)view->usable_percent);
    }
    else {
        snprintf(buf, sizeof(buf), "USABLE -- (EST)");
    }
    cdt_uii_set_text(lb.usable, buf);
}
