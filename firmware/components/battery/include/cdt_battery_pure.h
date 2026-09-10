/*
 * cdt_battery_pure.h — 电池采样纯逻辑（P4.4 固件侧，A3）
 *
 * 契约真源：
 *   docs/DEVELOPMENT_PLAN.md §7.1（每秒一批 9 次取中位、接近阈值转 1Hz、
 *   正常 10s、有效范围 2500–4500mV、连续 3 错 BATTERY_FAULT——连续计数归
 *   shared/power 的 FSM，本层只按样本标 valid）；
 *   docs/HARDWARE.md §2（GPIO4=ADC1_CH3、12bit+12dB、曲线拟合校准、×3 分压，
 *   分压系数为待本板核验的默认值）。
 *
 * 纯 C99：无 ESP-IDF/SDL/网络/文件 IO/浮点；分层门禁扫描本文件与 .c 的
 * include 行（scripts/build_firmware_io_tests.sh）。时间一律单调毫秒 int64。
 *
 * 校准与范围参数与 shared/power 的 Power FSM **同源**：直接复用
 * cdt_power_params_t 与 cdt_power_effective_mv（唯一公式真源），本层不另造
 * 第二套校准算式。校准归属（避免双算；cdt_power.h 契约"仅作用于阈值判定，
 * 不改写输入样本"）：
 *   - 驱动链路：ADC raw → 曲线拟合(pin mV) → ×分压 → 电池端 raw mV；
 *   - batch_to_sample 输出的 battery_mv = 电池端 raw（未乘 gain / 未加 offset）；
 *   - cal_gain_ppm / cal_offset_mv 在两处生效：本层用它对**校准后值**做范围
 *     判定得 valid；FSM step 内部对阈值判定再次应用——两处同源同值同式，
 *     校准非恒等时结果仍一致，不存在双乘。
 */
#ifndef CDT_BATTERY_PURE_H
#define CDT_BATTERY_PURE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cdt_power.h" /* shared/power：cdt_power_sample_t / params（同源）*/

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* §7.1 / HARDWARE §2 常量（集中复述，供驱动默认值与上层节奏调度）       */
/* ------------------------------------------------------------------ */
#define CDT_BATTERY_BATCH_N_DEFAULT 9u     /* §7.1：每秒一批 9 次取中位 */
#define CDT_BATTERY_BATCH_N_MAX 31u        /* 批量上限（防御；当前批量 9）*/
#define CDT_BATTERY_DIVIDER_PERMILLE_DEFAULT 3000u /* HARDWARE §2 ×3 分压（千分比整数，待本板核验）*/
#define CDT_BATTERY_PERIOD_NORMAL_MS 10000u /* §7.1：正常高电量每 10s 一批 */
#define CDT_BATTERY_PERIOD_FAST_MS 1000u    /* §7.1：接近阈值 1Hz */
#define CDT_BATTERY_FAST_BELOW_MV 3750u     /* §7.1：≤3.75V 转入 1Hz */

/* ------------------------------------------------------------------ */
/* 一次读取的结果（驱动侧逐次填充；mv 为电池端 raw mV）                  */
/* ------------------------------------------------------------------ */
typedef struct {
    uint16_t battery_mv; /* 电池端 mV = pin 校准 mV × 分压（未应用 gain/offset）*/
    bool     ok;         /* false = 本次 oneshot 读取或曲线拟合校准失败 */
} cdt_battery_reading_t;

/* ------------------------------------------------------------------ */
/* 纯函数                                                               */
/* ------------------------------------------------------------------ */

/* 中位数（内部拷贝后插入排序；n 奇数取正中位，偶数取下中位；n==0/NULL 出参
 * 缺失/n 超上限 → false 且不写 out）。 */
bool cdt_battery_median_mv(const uint16_t *mv, size_t n, uint16_t *out);

/* pin 校准 mV → 电池端 mV：pin × divider_permille / 1000（整数截断；permille
 * 由 HARDWARE §2 的 ×3 默认值给出 3000，真机核验后可改注入）。 */
uint16_t cdt_battery_pin_to_battery_mv(uint16_t pin_mv, uint32_t divider_permille);

/* §7.1 范围判定：对**校准后有效值**判 [valid_min_mv, valid_max_mv]（含边界）。
 * params 为 FSM 同源参数（NULL → false）。 */
bool cdt_battery_mv_in_range(int32_t effective_mv, const cdt_power_params_t *params);

/* §7.1 采样节奏辅助：最近有效样本 ≤ fast_below_mv → true（上层转 1Hz 快采）；
 * 否则维持 10s 常规节奏。节奏调度本身归上层（本层不做定时）。
 * 组件侧单参便捷封装见 cdt_battery.h 的 cdt_battery_should_sample_fast。 */
bool cdt_battery_should_sample_fast_mv(uint16_t last_valid_mv, uint16_t fast_below_mv);

/* 一批读取 → cdt_power_sample_t（喂 shared/power FSM 的形状）。规则：
 *   - n==0 / 出参缺失 / n 超上限 → false（不写 out）；
 *   - 任一读数 !ok → 整批 unknown：valid=false、battery_mv=0（保守：不用半批
 *     数据冒充可信电压；连续计数由 FSM 按 invalid 样本计，驱动不另记）；
 *   - 全 ok → 取中位 → cdt_power_effective_mv（同源公式）→ 范围判定：
 *       范围内  → valid=true，battery_mv=raw 中位（FSM 契约：不预乘校准）；
 *       范围外  → valid=false，battery_mv 保留 raw 中位（诊断分压接线用；
 *                  FSM 对 invalid 样本不读 mv，遥测层按契约置 null）。
 * at_ms 由调用方传入单调毫秒。 */
bool cdt_battery_batch_to_sample(const cdt_battery_reading_t *reads, size_t n,
                                 const cdt_power_params_t *params,
                                 int64_t at_ms, cdt_power_sample_t *out);

#ifdef __cplusplus
}
#endif

#endif /* CDT_BATTERY_PURE_H */
