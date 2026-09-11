/*
 * main.c — power_skeleton 独立工程接线示例（P5.3 固件侧，A3）
 *
 * 目的：把 firmware/components/power 执行器编入独立目标并完成最小接线
 * （编译级验证；不烧录、不上板）。§7.3 停止顺序由 cdt_power_exec 编排：
 *   阻止新更新 → 末帧（≤1s，失败也要睡）→ 停 transport/无线 → 关外设 →
 *   LCD 保持/关闭 → GPIO 安全电平（PA=46 拉低；本板无背光脚）→
 *   RTC 低压原因标记 + 配唤醒源 → esp_deep_sleep_start()。
 *
 * 诚实边界：
 *   - stop_transport_radio / power_off_periph / lcd_final 目前是日志桩：
 *     真实 ST7305 末帧与保持命令、无线停机接线归 P4.3/P5.2 集成；
 *   - 末帧 poll_flush 在本骨架直接返回完成（真实 flush 句柄随显示接线）；
 *   - 深睡电流 / LCD 保持电流 / 深睡保留 / KEY 唤醒行为零结论，归 P5.4
 *     真机实测（HARDWARE §4：KEY 深睡唤醒 unverified，CDT_CFG_KEY_DEEP_WAKE
 *     默认 0，本工程不改 sdkconfig 开启）；
 *   - CRITICAL 触发源（P5.1 FSM + P4.4 采样调度）归 main 域集成，此处直接
 *     以 FSM 冻结动作演示执行器顺序，不冒充真实采样链路。
 */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cdt_power_exec.h"

#define TAG "cdt_pwr_skel"

/* ---------------- 平台注入：等待/时钟（IDF 实现示例）---------------- */

static void wait_ms_impl(void *user, uint32_t ms)
{
    (void)user;
    vTaskDelay(pdMS_TO_TICKS(ms)); /* 1ms 粒度（CONFIG_FREERTOS_HZ=1000）*/
}

static int64_t now_ms_impl(void *user)
{
    (void)user;
    return esp_timer_get_time() / 1000;
}

/* 末帧 poll：骨架无显示接线，直接返回完成；真实接法 = flush 完成信号量
 * 非阻塞查询（xSemaphoreTake(0)），超时路径由执行器 1ms 粒度轮询兜底。 */
static bool poll_flush_impl(void *user)
{
    (void)user;
    return true;
}

/* ---------------- 平台注入：§7.3 步骤回调（当前为日志桩）------------ */

static void on_cancel_reconnect(void *user)
{
    (void)user;
    /* P5.2 接线点：取消 transport 重连退避/定时器（不直接依赖 transport 组件）*/
    ESP_LOGW(TAG, "[桩] 步骤① 取消重连（P5.2 接 transport 后生效）");
}

static void on_stop_transport_radio(void *user)
{
    (void)user;
    ESP_LOGW(TAG, "[桩] 步骤③ 停 transport/无线（P5.2 接线）");
}

static void on_power_off_periph(void *user)
{
    (void)user;
    ESP_LOGW(TAG, "[桩] 步骤④ 关外设（音频/传感器）");
}

static void on_lcd_final(void *user, cdt_power_exec_lcd_mode_t mode)
{
    (void)user;
    /* P4.3/P5.4 接线点：ST7305 保持（自刷新）/关闭命令；保持/关闭默认值
     * 等两者的整板电流对照实测后再定（AGENTS.md：不显示编造效果）。 */
    ESP_LOGW(TAG, "[桩] 步骤⑤ LCD 末态=%s（P4.3/P5.4 接线与对照实测）",
             mode == CDT_POWER_EXEC_LCD_HOLD ? "保持" : "关闭");
}

void app_main(void)
{
    static const cdt_power_exec_prepare_ops_t prep_ops = {
        NULL, on_cancel_reconnect
    };
    static const cdt_power_exec_wait_ops_t wait_ops = {
        NULL, poll_flush_impl, wait_ms_impl, now_ms_impl
    };
    static const cdt_power_exec_sleep_ops_t sleep_ops = {
        NULL, on_stop_transport_radio, on_power_off_periph, on_lcd_final
    };
    cdt_power_exec_sleep_config_t sleep_cfg;
    cdt_power_exec_stats_t st;
    bool low_hint = false;
    bool allow_radio;

    /* §7.3 BOOT_CHECK：先读 RTC 低压原因标记（门禁），再谈无线。
     * 深睡后 PWR 重新上电 → 标记随 RTC 断电清零 → 正常启动判定。 */
    allow_radio = cdt_power_exec_boot_gate_allow_radio(
        cdt_power_exec_rtc_reason_io(), &low_hint);
    ESP_LOGI(TAG, "BOOT_CHECK 门禁：%s开无线（rtc 低压标记 hint=%d）",
             allow_radio ? "允" : "拒", (int)low_hint);
    /* low_hint 即 cdt_power_init(&fsm, &params, low_hint) 的注入参数
     * （P5.1 FSM 要求低压唤醒 recovery 后才恢复，真实接线归 main 集成）。 */

    /* 演示：以 P5.1 FSM 冻结期动作驱动执行器走完整 §7.3 顺序。 */
    cdt_power_exec_flow_reset(CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);

    cdt_power_exec_prepare(
        CDT_POWER_ACT_ENTER_CRITICAL | CDT_POWER_ACT_BEGIN_SLEEP_PREP,
        &prep_ops);

    (void)cdt_power_exec_final_frame(&wait_ops,
                                     CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);

    memset(&sleep_cfg, 0, sizeof(sleep_cfg));
    sleep_cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD; /* 保持/关闭对照归 P5.4 实测 */
    sleep_cfg.key_deep_wake = false; /* 宏默认 0（HARDWARE §4 unverified）*/
    sleep_cfg.low_battery_reason = true;
    cdt_power_exec_sleep(&sleep_cfg, &sleep_ops,
                         cdt_power_exec_rtc_reason_io());

    cdt_power_exec_stats_get(&st);
    ESP_LOGI(TAG, "执行统计：frozen=%d cancelled=%d flushed=%d timeout=%d "
                  "abandoned=%d mark=%d violation=%d",
             (int)st.updates_frozen, (int)st.reconnect_cancelled,
             (int)st.final_frame_flushed, (int)st.final_frame_timed_out,
             (int)st.final_frame_abandoned, (int)st.reason_written,
             (int)st.order_violation);

    ESP_LOGW(TAG, "esp_deep_sleep_start() 已调用——真机不返回；以下仅异常环境可达");
    /* 真机流程终点即深睡；PWR 重新上电后按重启处理（§7.3 DEEP_SLEEP 行）。 */
}
