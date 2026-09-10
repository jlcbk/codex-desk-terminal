/*
 * cdt_battery.h — GPIO4 电池 ADC 采样组件（P4.4 固件侧，A3）
 *
 * 契约真源：docs/DEVELOPMENT_PLAN.md §7.1/§P4.4 行、docs/HARDWARE.md §2、
 * docs/INTERFACES.md §5 Battery HAL 行（read_sample→mv/valid/error；统一校准；
 * 不得有无界阻塞）。
 *
 * 硬件（HARDWARE §2，均 confirmed）：
 *   BAT_ADC = GPIO4 = ADC1_CH3，外接 1/3 分压；ADC1 + 12bit + 12dB +
 *   曲线拟合校准（adc_cali_create_scheme_curve_fitting），配置时序照官方
 *   03_ADC_Test 的 adc_bsp（vendor HEAD eb1f634）。分压后满电约 1.4V 落在
 *   12dB 量程内。
 *
 * 边界（AGENTS.md / 计划）：
 *   - 本组件只产出 cdt_power_sample_t；故障"连续 3 次失败→BATTERY_FAULT"的
 *     连续计数与状态转移归 shared/power 的 FSM，本组件只按样本标 valid；
 *   - 采样节奏（10s 常规 / 1Hz 近阈值）由上层调度，本组件提供
 *     cdt_battery_should_sample_fast 辅助，不内置定时器；
 *   - 校准注入 cal_offset_mv/cal_gain_ppm 与 FSM 参数**同源**（同一
 *     cdt_power_params_t 实例喂两边；换算唯一入口 cdt_power_effective_mv）。
 *     P4.4 真机表计定标后只回填参数值，不改链路结构；本交付**无任何真机
 *     校准结论**，默认恒等（gain=1000000ppm、offset=0）；
 *   - 高阻分压稳定时间/采样误差 HARDWARE §2 标 unverified → 9 次连读之间
 *     暂不插入延时，实测归真机校准任务（剩余问题清单）。
 *
 * 单实例设计：板上仅 BAT_ADC 一路模拟输入；init 成功前调用 sample_batch
 * 返回 ESP_ERR_INVALID_STATE。
 */
#ifndef CDT_BATTERY_H
#define CDT_BATTERY_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#include "cdt_power.h"         /* shared/power：FSM 同源参数/样本形状 */
#include "cdt_battery_pure.h"  /* 纯逻辑（中位/范围/节奏），主机端可测 */

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 配置（init 时拷贝快照；后续不改）                                     */
/* ------------------------------------------------------------------ */
typedef struct {
    uint8_t batch_n;          /* 每批读取次数：0→默认 9；>CDT_BATTERY_BATCH_N_MAX 拒绝 */
    uint32_t divider_permille;/* 分压千分比：0→默认 3000（×3，待本板核验）*/
    cdt_power_params_t params;/* FSM 同源参数：校准 gain/offset + 有效范围；
                               * 同一实例须同时用于 cdt_power_init（同源契约）*/
} cdt_battery_config_t;

/* ADC1 + 曲线拟合校准 + 通道配置（时序照 vendor 03_ADC_Test adc_bsp）。
 * 成功后组件持有 oneshot/cali 句柄；重复 init → ESP_ERR_INVALID_STATE。 */
esp_err_t cdt_battery_init(const cdt_battery_config_t *cfg);

/* 采样一批 batch_n 次 → 中位样本（cdt_power_sample_t 形状）。
 * 返回值 = 驱动调用级状态：全部读取失败 → 返回最后一次 esp_err 且 *out 为
 * valid=false（mv=0）；部分失败 → ESP_OK 但 *out.valid=false（§7.1 保守：
 * 任一失败整批 unknown）；全成功 → ESP_OK，valid 由范围判定决定。
 * now_ms 由调用方传单调毫秒（与 FSM step 的 now 同源时钟）。无界阻塞无：
 * oneshot 单次读取为 µs 级阻塞。 */
esp_err_t cdt_battery_sample_batch(int64_t now_ms, cdt_power_sample_t *out);

/* 最近一次驱动级错误（诊断用；ESP_OK = 自上次成功读以来无错误）。 */
esp_err_t cdt_battery_last_error(void);

/* §7.1 节奏辅助（默认阈值 3750mV）：true → 上层按 1Hz 调度；false → 10s。
 * 仅在存在有效样本后调用；"从未有样本"时由上层用常规节奏起步。 */
bool cdt_battery_should_sample_fast(uint16_t last_valid_mv);

#ifdef __cplusplus
}
#endif

#endif /* CDT_BATTERY_H */
