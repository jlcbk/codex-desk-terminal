/*
 * cdt_key.h — KEY(GPIO18)/BOOT(GPIO0) 按键组件（P4.5 固件侧，A3）
 *
 * 契约真源：docs/DEVELOPMENT_PLAN.md §1/§6/P4.5 行、docs/HARDWARE.md §1.2
 * （均 confirmed：BOOT=GPIO0、KEY=GPIO18，低有效 + 内部上拉）。
 *
 * 去抖/长短按语义全部在 cdt_key_pure（§6：去抖 30ms、长按 800ms、松开时
 * 判定、长按不再触发短按）；本文件只做 GPIO + 周期轮询接线（编译级验证，
 * 未上板）。
 *
 * 事件语义归上层（本组件不解释业务）：
 *   - KEY_SHORT → 页面轮换（NOW→AGENTS→PLAN→USAGE，§6）；
 *   - KEY_LONG  → 本地 acknowledge/mute（只静音当前提醒，§6）；亦为 BLE
 *     数字比较确认候选（P3.4 B2 真机项，当前默认拒绝未接线）；
 *   - BOOT_SHORT/BOOT_LONG → 保留（§1：BOOT 保留下载用途）。
 *
 * 回调上下文：esp_timer 任务。回调内禁止直接操作 UI/LVGL（AGENTS.md 边界：
 * UI 只在 LVGL 所属任务操作），只允许入队/置位后由 runtime 任务消费。
 *
 * GPIO0 为 strapping 脚：仅配置为输入 + 内部上拉（不改 JTAG/启动相关
 * strapping 行为，与官方 button_bsp 同配置）；深睡唤醒能力未验证
 * （HARDWARE §4.2：KEY 深睡唤醒 unverified，保底 PWR 上电），本组件不做
 * 任何睡眠唤醒配置。
 */
#ifndef CDT_KEY_H
#define CDT_KEY_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#include "cdt_key_pure.h" /* 纯去抖/长短按状态机，主机端可测 */

#ifdef __cplusplus
extern "C" {
#endif

/* 引脚（HARDWARE §1.2 confirmed；集中复述，改动须先改 HARDWARE 审计） */
#define CDT_KEY_GPIO_KEY 18u  /* KEY：低有效 + 内部上拉 */
#define CDT_KEY_GPIO_BOOT 0u  /* BOOT：低有效 + 内部上拉（strapping 注意，见文件头）*/

/* 输出事件枚举（来源 × 种类）。
 * 整机集成（A3+A4）更名 cdt_key_event_t → cdt_key_hw_event_t：与
 * shared/ui/cdt_nav.h 的 UI 层 cdt_key_event_t（CDT_KEY_SHORT_PRESS 等，
 * P0 冻结面）同名不同形，同一 TU（main.c）无法同时包含两处定义；
 * 枚举值/事件语义不变。 */
typedef enum {
    CDT_KEY_EVENT_NONE = 0,
    CDT_KEY_EVENT_KEY_SHORT = 1,  /* KEY 短按 → 上层映射轮页（§6）*/
    CDT_KEY_EVENT_KEY_LONG = 2,   /* KEY 长按 → 上层映射静音/确认（§6；B2 候选）*/
    CDT_KEY_EVENT_BOOT_SHORT = 3, /* BOOT 短按 → 保留（§1 下载用途）*/
    CDT_KEY_EVENT_BOOT_LONG = 4   /* BOOT 长按 → 保留 */
} cdt_key_hw_event_t;

/* 事件名（日志用短名） */
const char *cdt_key_event_name(cdt_key_hw_event_t ev);

/* ------------------------------------------------------------------ */
/* 配置（start 时拷贝快照）                                             */
/* ------------------------------------------------------------------ */
typedef struct {
    uint32_t debounce_ms;   /* 0 → 默认 30ms（§6）*/
    uint32_t long_press_ms; /* 0 → 默认 800ms（§6）*/
    uint32_t poll_period_ms;/* 轮询周期：0 → 默认 5ms（去抖分辨率；官方
                             * button_bsp 同为 5ms tick 轮询模式）*/
    void (*on_event)(void *user, cdt_key_hw_event_t ev); /* 必填；esp_timer 上下文 */
    void *user;
} cdt_key_config_t;

/* 配置 GPIO（输入+上拉）并启动 5ms 轮询定时器。两键各持一个纯状态机实例。
 * 重复 start（未 stop）→ ESP_ERR_INVALID_STATE。 */
esp_err_t cdt_key_start(const cdt_key_config_t *cfg);

/* 停止轮询并删除定时器（GPIO 保持输入态）。未 start 时返回 ESP_ERR_INVALID_STATE。 */
esp_err_t cdt_key_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_KEY_H */
