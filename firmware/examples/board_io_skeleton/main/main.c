/*
 * main.c — board_io_skeleton 独立工程的接线示例（P4.4+P4.5 固件侧，A3）
 *
 * 目的：把 firmware/components/battery 与 firmware/components/input 编入同一
 * 构建目标并完成最小接线（编译级验证；不烧录、不上板）。
 *
 * 上层组装边界（AGENTS.md / INTERFACES §5）：
 *   - 采样节奏（10s 常规 / 1Hz 近阈值）归本层调度器（后续 runtime 集成），
 *     组件只给 cdt_battery_should_sample_fast 辅助；本桩仅示范一批采样；
 *   - cdt_power_sample_t 喂 shared/power 的 FSM（同源 params：同一实例喂
 *     cdt_power_init 与 cdt_battery_init——校准注入只回填数值）；
 *   - KEY_SHORT 轮页 / KEY_LONG 静音的语义映射归 DeviceRuntime/UI 集成（§6），
 *     本桩仅计数日志，不冒充已接入页面；BLE 数字比较确认（B2）同理未接。
 *   - key 回调运行于 esp_timer 任务：真实集成只允许入队/置位，UI 操作必须
 *     在 LVGL 任务（组件头文件红线复述；本桩仅 ESP_LOGI 计数）。
 */
#include <inttypes.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "cdt_battery.h"
#include "cdt_key.h"
#include "cdt_power.h"

#define TAG "cdt_board_io"

static volatile uint32_t s_key_event_count;

static void on_key_event(void *user, cdt_key_hw_event_t ev)
{
    (void)user;
    s_key_event_count++;
    ESP_LOGI(TAG, "key event %s（语义映射归上层；计数=%" PRIu32 "）",
             cdt_key_event_name(ev), s_key_event_count);
}

void app_main(void)
{
    /* ① 与 FSM 同源的参数/校准（§7.1/§7.2 初值；P4.4 真机定标后只回填
     *    cal_gain_ppm/cal_offset_mv，两个组件喂同一实例） */
    cdt_power_params_t params;
    cdt_power_params_init(&params);

    cdt_battery_config_t bcfg;
    bcfg.batch_n = CDT_BATTERY_BATCH_N_DEFAULT;                    /* 每批 9 次（§7.1）*/
    bcfg.divider_permille = CDT_BATTERY_DIVIDER_PERMILLE_DEFAULT;  /* ×3 待本板核验 */
    bcfg.params = params;
    esp_err_t rc = cdt_battery_init(&bcfg);
    ESP_LOGI(TAG, "cdt_battery_init rc=%s", esp_err_to_name(rc));

    /* ② 批量采样一批 → 喂 shared/power FSM 一步（形状与链接验证；首次样本
     *    决定 BOOT_CHECK 启动判定由 FSM 完成，本桩不解释动作位） */
    cdt_power_fsm_t fsm;
    cdt_power_init(&fsm, &params, false);
    cdt_power_sample_t smp;
    rc = cdt_battery_sample_batch((int64_t)(esp_timer_get_time() / 1000), &smp);
    ESP_LOGI(TAG, "sample_batch rc=%s mv=%u valid=%d last_err=%s",
             esp_err_to_name(rc), (unsigned)smp.battery_mv,
             (int)smp.valid, esp_err_to_name(cdt_battery_last_error()));

    cdt_power_input_t in;
    in.kind = CDT_POWER_IN_SAMPLE;
    in.sample = smp;
    in.event = CDT_POWER_EVT_NONE;
    {
        char abuf[96];
        cdt_power_action_t act =
            cdt_power_step(&fsm, &in, (int64_t)(esp_timer_get_time() / 1000));
        ESP_LOGI(TAG, "power step act=%s state=%s",
                 cdt_power_actions_str(abuf, sizeof(abuf), act),
                 cdt_power_state_str(fsm.state));
    }

    /* ③ KEY(GPIO18)/BOOT(GPIO0) 轮询接线（去抖/长短按判定在纯层；真机按压
     *    手感/30ms 充分性实测归后续交互任务） */
    cdt_key_config_t kcfg;
    kcfg.debounce_ms = 0;    /* 0 → 默认 30ms（§6）*/
    kcfg.long_press_ms = 0;  /* 0 → 默认 800ms（§6）*/
    kcfg.poll_period_ms = 0; /* 0 → 默认 5ms */
    kcfg.on_event = on_key_event;
    kcfg.user = NULL;
    rc = cdt_key_start(&kcfg);
    ESP_LOGI(TAG, "cdt_key_start rc=%s", esp_err_to_name(rc));

    ESP_LOGI(TAG, "board_io_skeleton 接线完成（编译级验证，不上板）");
}
