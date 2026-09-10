/*
 * cdt_battery_pure.c — 电池采样纯逻辑实现（P4.4 固件侧，A3）
 *
 * 规则真源见 cdt_battery_pure.h 文件头（开发计划 §7.1、HARDWARE §2）。
 * 纯 C99：无平台头、无浮点、无 IO；校准换算唯一入口为 shared/power 的
 * cdt_power_effective_mv（与 FSM 同源，不另写第二套公式）。
 */
#include "cdt_battery_pure.h"

/* ---------------- 中位数 ---------------- */

bool cdt_battery_median_mv(const uint16_t *mv, size_t n, uint16_t *out)
{
    uint16_t tmp[CDT_BATTERY_BATCH_N_MAX];
    size_t i;
    size_t j;

    if (mv == NULL || out == NULL || n == 0u || n > (size_t)CDT_BATTERY_BATCH_N_MAX) {
        return false;
    }
    for (i = 0u; i < n; i++) {
        /* 插入排序（批量 ≤31，O(n²) 足够；不改调用方缓冲） */
        uint16_t key = mv[i];
        j = i;
        while (j > 0u && tmp[j - 1u] > key) {
            tmp[j] = tmp[j - 1u];
            j--;
        }
        tmp[j] = key;
    }
    /* 奇数取正中位；偶数取下中位（避免引入除 2 后向上取整的歧义） */
    *out = tmp[(n - 1u) / 2u];
    return true;
}

/* ---------------- 分压还原 ---------------- */

uint16_t cdt_battery_pin_to_battery_mv(uint16_t pin_mv, uint32_t divider_permille)
{
    uint32_t v = (uint32_t)pin_mv;
    if (divider_permille == 0u) {
        divider_permille = CDT_BATTERY_DIVIDER_PERMILLE_DEFAULT;
    }
    v = v * divider_permille / 1000u;
    /* pin mV ≤ 3100（12dB 量程）、permille 典型 3000 → 结果 ≤ 9300，不会回绕 */
    return (uint16_t)v;
}

/* ---------------- 范围判定（对校准后有效值） ---------------- */

bool cdt_battery_mv_in_range(int32_t effective_mv, const cdt_power_params_t *params)
{
    if (params == NULL) {
        return false;
    }
    return effective_mv >= (int32_t)params->valid_min_mv &&
           effective_mv <= (int32_t)params->valid_max_mv;
}

/* ---------------- 采样节奏辅助 ---------------- */

bool cdt_battery_should_sample_fast_mv(uint16_t last_valid_mv, uint16_t fast_below_mv)
{
    /* §7.1：接近阈值（≤ fast_below_mv，默认 3750）持续 1Hz 检查 */
    return last_valid_mv <= fast_below_mv;
}

/* ---------------- 批次 → FSM 样本 ---------------- */

bool cdt_battery_batch_to_sample(const cdt_battery_reading_t *reads, size_t n,
                                 const cdt_power_params_t *params,
                                 int64_t at_ms, cdt_power_sample_t *out)
{
    uint16_t raw[CDT_BATTERY_BATCH_N_MAX];
    uint16_t median;
    int32_t effective;
    size_t i;

    if (reads == NULL || params == NULL || out == NULL ||
        n == 0u || n > (size_t)CDT_BATTERY_BATCH_N_MAX) {
        return false;
    }

    /* 任一失败 → 整批 unknown（§7.1：驱动失败标记 unknown；连续计数归 FSM）*/
    for (i = 0u; i < n; i++) {
        if (!reads[i].ok) {
            out->battery_mv = 0u;
            out->valid = false;
            out->at_ms = at_ms;
            return true;
        }
        raw[i] = reads[i].battery_mv;
    }

    if (!cdt_battery_median_mv(raw, n, &median)) {
        return false; /* 不可达（n 已校验），防御 */
    }

    /* 范围判定用校准后值（同源 cdt_power_effective_mv）；
     * 输出 battery_mv 保持 raw 中位——FSM 在阈值判定时自行应用 gain/offset，
     * 此处预乘会造成双算（cdt_power.h："不改写输入样本"）。 */
    effective = cdt_power_effective_mv(params, median);
    out->battery_mv = median;
    out->at_ms = at_ms;
    if (!cdt_battery_mv_in_range(effective, params)) {
        /* §7.1：范围外标记 unknown；保留 raw 中位供分压接线诊断 */
        out->valid = false;
    } else {
        out->valid = true;
    }
    return true;
}
