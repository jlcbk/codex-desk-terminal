/*
 * test_battery_pure.c — P4.4 电池纯逻辑主机端测试（A3）
 *
 * 用法：test_battery_pure（无参数；真实退出码，任何 FAIL → exit 1）。
 * 真源：docs/DEVELOPMENT_PLAN.md §7.1、docs/HARDWARE.md §2、
 * shared/power/cdt_power.h（校准/范围参数同源契约）。
 *
 * 覆盖：中位数（奇/偶/重复/单点/空/NULL）、分压还原（×3/恒等/截断）、
 * 节奏决策（3750 边界）、范围判定（2500/4500 边界）、校准换算同源
 * （与 cdt_power_effective_mv 直连比对）、批次→样本（全 ok / 部分失败 /
 * 全失败 / 范围外 / 校准不预乘 / at_ms 透传 / 用量错误）。
 */
#include <stdio.h>

#include "cdt_battery_pure.h"

static int failures = 0;
static int passes = 0;

static void check(int cond, const char *name, const char *detail)
{
    if (cond) {
        passes++;
        printf("[PASS] %s\n", name);
    } else {
        failures++;
        printf("[FAIL] %s — %s\n", name, detail ? detail : "");
    }
}

static cdt_power_params_t base_params(void)
{
    cdt_power_params_t p;
    cdt_power_params_init(&p); /* §7.1/§7.2 初值：2500–4500、gain=1e6、offset=0 */
    return p;
}

/* ---------- 中位数 ---------- */

static void test_median_odd9(void)
{
    /* 乱序 9 点，中位应为排序后第 5 个 = 3700 */
    static const uint16_t v[9] = { 3700, 3690, 3710, 3680, 3720, 3670, 3730, 3660, 3740 };
    uint16_t m = 0;
    bool ok = cdt_battery_median_mv(v, 9u, &m);
    check(ok && m == 3700u, "median_odd9_shuffled", "应为 3700");
}

static void test_median_n1_even_repeats(void)
{
    uint16_t one = 3900;
    uint16_t even[4] = { 10, 30, 20, 40 };   /* 排序 10/20/30/40 → 下中位 20 */
    uint16_t rep[9] = { 3500, 3500, 3500, 3500, 3500, 3500, 3500, 3500, 3500 };
    uint16_t m1 = 0;
    uint16_t m2 = 0;
    uint16_t m3 = 0;
    bool ok0 = cdt_battery_median_mv(&one, 0u, &m1);
    bool ok1 = cdt_battery_median_mv(&one, 1u, &m1);
    bool ok2 = cdt_battery_median_mv(even, 4u, &m2);
    bool ok3 = cdt_battery_median_mv(rep, 9u, &m3);
    check(!ok0, "median_n0_rejected", "n=0 应拒绝");
    check(ok1 && m1 == 3900u, "median_n1", "单点直取 3900");
    check(ok2 && m2 == 20u, "median_even_lower_middle", "偶数取下中位 20");
    check(ok3 && m3 == 3500u, "median_all_repeat", "全重复取同值");
}

static void test_median_usage_errors(void)
{
    uint16_t v[3] = { 1, 2, 3 };
    uint16_t m = 999;
    bool ok_null_out = cdt_battery_median_mv(v, 3u, NULL);
    bool ok_null_in = cdt_battery_median_mv(NULL, 3u, &m);
    bool ok_over = cdt_battery_median_mv(v, (size_t)CDT_BATTERY_BATCH_N_MAX + 1u, &m);
    check(!ok_null_out && !ok_null_in && !ok_over && m == 999u,
          "median_usage_errors_no_write", "NULL/超限拒绝且不写出参");
}

/* ---------- 分压还原 ---------- */

static void test_pin_to_battery(void)
{
    uint16_t a = cdt_battery_pin_to_battery_mv(1240u, 3000u); /* ×3 → 3720 */
    uint16_t b = cdt_battery_pin_to_battery_mv(0u, 3000u);
    uint16_t c = cdt_battery_pin_to_battery_mv(3100u, 1000u); /* 恒等 */
    uint16_t d = cdt_battery_pin_to_battery_mv(1234u, 3333u); /* 4112.922 → 截断 4112 */
    check(a == 3720u, "divider_x3", "1240×3=3720");
    check(b == 0u, "divider_zero", "0→0");
    check(c == 3100u, "divider_identity_permille", "permille 1000 恒等");
    check(d == 4112u, "divider_truncation", "整数截断 4112");
}

/* ---------- 节奏决策 ---------- */

static void test_should_sample_fast(void)
{
    bool a = cdt_battery_should_sample_fast_mv(3749u, 3750u);
    bool b = cdt_battery_should_sample_fast_mv(3750u, 3750u); /* ≤3.75V 含边界 */
    bool c = cdt_battery_should_sample_fast_mv(3751u, 3750u);
    bool d = cdt_battery_should_sample_fast_mv(3600u, 3600u); /* 自定义阈值同样含边界 */
    bool e = cdt_battery_should_sample_fast_mv(3601u, 3600u);
    check(a && b, "fast_below_3750_inclusive", "3749/3750 → 1Hz");
    check(!c, "fast_above_3750_normal", "3751 → 10s 常规");
    check(d && !e, "fast_custom_threshold_boundary", "自定义 3600 边界含 ≤");
}

/* ---------- 范围判定（校准后值） ---------- */

static void test_range_boundaries(void)
{
    cdt_power_params_t p = base_params();
    check(cdt_battery_mv_in_range(2500, &p), "range_min_2500_inclusive", "2500 有效");
    check(!cdt_battery_mv_in_range(2499, &p), "range_2499_invalid", "2499 无效");
    check(cdt_battery_mv_in_range(4500, &p), "range_max_4500_inclusive", "4500 有效");
    check(!cdt_battery_mv_in_range(4501, &p), "range_4501_invalid", "4501 无效");
    check(!cdt_battery_mv_in_range(3700, NULL), "range_null_params", "NULL params 拒绝");
}

/* ---------- 校准同源 ---------- */

static void test_calibration_same_source(void)
{
    /* P4.4 定标注入形状：非恒等 gain/offset；换算必须与 FSM 直连调用一致 */
    cdt_power_params_t p = base_params();
    p.cal_gain_ppm = 1002000; /* ×1.002 */
    p.cal_offset_mv = 10;
    int32_t via_power = cdt_power_effective_mv(&p, 3700); /* 3707.4→3707 +10 = 3717 */
    check(via_power == 3717, "power_effective_expected", "FSM 公式基准 3717");

    /* raw 2490 校准后 2494+10=2504 落入范围 → valid（判定对校准后值）*/
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;
    uint8_t i;
    for (i = 0u; i < 9u; i++) {
        reads[i].battery_mv = 2490u;
        reads[i].ok = true;
    }
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 1000, &out) && out.valid,
          "calibrated_range_check_valid", "2490+校准→2504 范围内有效");

    /* raw 2480：校准后 2484+10=2494 <2500 → invalid（且保留 raw 供诊断）*/
    for (i = 0u; i < 9u; i++) {
        reads[i].battery_mv = 2480u;
    }
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 1000, &out) && !out.valid &&
              out.battery_mv == 2480u,
          "calibrated_range_check_invalid_keeps_raw", "校准后越界无效且保留 raw");
}

/* ---------- 批次 → 样本 ---------- */

static void fill_ok(cdt_battery_reading_t *reads, uint8_t n, uint16_t mv)
{
    uint8_t i;
    for (i = 0u; i < n; i++) {
        reads[i].battery_mv = mv;
        reads[i].ok = true;
    }
}

static void test_batch_all_ok(void)
{
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9] = {
        { 3660, true }, { 3740, true }, { 3670, true }, { 3730, true }, { 3700, true },
        { 3680, true }, { 3720, true }, { 3690, true }, { 3710, true }
    }; /* 中位 3700 */
    cdt_power_sample_t out;
    bool ok = cdt_battery_batch_to_sample(reads, 9u, &p, 12345, &out);
    check(ok && out.valid && out.battery_mv == 3700u && out.at_ms == 12345,
          "batch_all_ok_median_at", "中位 3700、at_ms 透传");
}

static void test_batch_any_fail_invalid(void)
{
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;
    uint8_t i;
    fill_ok(reads, 9u, 3950u);
    reads[4].ok = false; /* 1/9 驱动失败 → 整批 unknown（保守）*/
    bool ok = cdt_battery_batch_to_sample(reads, 9u, &p, 2000, &out);
    check(ok && !out.valid && out.battery_mv == 0u,
          "batch_any_fail_unknown_zero", "任一失败→valid=false 且 mv=0");

    for (i = 0u; i < 9u; i++) {
        reads[i].ok = false;
    }
    ok = cdt_battery_batch_to_sample(reads, 9u, &p, 2000, &out);
    check(ok && !out.valid && out.battery_mv == 0u, "batch_all_fail_unknown", "全失败→unknown");
}

static void test_batch_out_of_range(void)
{
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;

    fill_ok(reads, 9u, 1200u); /* 分压接线故障形态：1200mV */
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 3000, &out) && !out.valid &&
              out.battery_mv == 1200u,
          "range_out_low_keeps_mv", "1200 越界 unknown 且保留 mv 诊断");

    fill_ok(reads, 9u, 4600u);
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 3000, &out) && !out.valid &&
              out.battery_mv == 4600u,
          "range_out_high_keeps_mv", "4600 越界 unknown");

    fill_ok(reads, 9u, 2500u);
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 3000, &out) && out.valid,
          "range_edge_2500_valid", "2500 边界有效");
    fill_ok(reads, 9u, 4500u);
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 3000, &out) && out.valid,
          "range_edge_4500_valid", "4500 边界有效");
}

static void test_batch_no_double_calibration(void)
{
    /* 契约：样本 battery_mv = raw 中位（FSM 阈值判定时自行应用校准）；
     * 非恒等校准下输出必须仍是 raw，否则 FSM 双算。 */
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;
    p.cal_gain_ppm = 1002000;
    p.cal_offset_mv = 10;
    fill_ok(reads, 9u, 3700u);
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 0, &out) && out.valid &&
              out.battery_mv == 3700u && out.battery_mv != 3717u,
          "batch_output_raw_not_effective", "输出 raw 3700（非校准后 3717）");
}

static void test_batch_usage_errors(void)
{
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;
    fill_ok(reads, 9u, 3700u);
    check(!cdt_battery_batch_to_sample(reads, 0u, &p, 0, &out), "batch_n0_rejected", "n=0 拒绝");
    check(!cdt_battery_batch_to_sample(NULL, 9u, &p, 0, &out), "batch_null_rejected", "NULL 拒绝");
    check(!cdt_battery_batch_to_sample(reads, (size_t)CDT_BATTERY_BATCH_N_MAX + 1u, &p, 0, &out),
          "batch_over_max_rejected", "超上限拒绝");
}

/* ---------- §7.1 与 FSM 联动形状（同源编译单元验证） ---------- */

static void test_sample_feeds_power_params_shape(void)
{
    /* 输出形状可直接作为 cdt_power_input_t.sample（编译期即证：同类型赋值）*/
    cdt_power_params_t p = base_params();
    cdt_battery_reading_t reads[9];
    cdt_power_sample_t out;
    cdt_power_input_t in;
    fill_ok(reads, 9u, 3590u);
    check(cdt_battery_batch_to_sample(reads, 9u, &p, 777, &out) && out.valid,
          "sample_3590_valid", "低电压样本有效（保护判定归 FSM）");
    in.kind = CDT_POWER_IN_SAMPLE;
    in.sample = out;
    in.event = CDT_POWER_EVT_NONE;
    check(in.sample.battery_mv == 3590u && in.sample.at_ms == 777,
          "sample_shape_assignable", "cdt_power_sample_t 形状直通");
}

int main(void)
{
    test_median_odd9();
    test_median_n1_even_repeats();
    test_median_usage_errors();
    test_pin_to_battery();
    test_should_sample_fast();
    test_range_boundaries();
    test_calibration_same_source();
    test_batch_all_ok();
    test_batch_any_fail_invalid();
    test_batch_out_of_range();
    test_batch_no_double_calibration();
    test_batch_usage_errors();
    test_sample_feeds_power_params_shape();

    printf("test_battery_pure: %d PASS / %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
