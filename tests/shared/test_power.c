/*
 * test_power.c — P5.1 共享纯 Power FSM 主机端测试（A3）
 *
 * 用法：test_power（无参数；虚拟单调时钟 + 电压 trace，无需真实等待 30s）。
 * 真源：docs/DEVELOPMENT_PLAN.md §7.1/§7.2/§7.3、§8 测试策略 "Power FSM" 行、
 * docs/INTERFACES.md §4、docs/HARDWARE.md §4（唤醒保底 = PWR 重新上电）。
 *
 * §8 Power FSM 行必测案例 → 测试函数映射（任务书补充案例一并标注）：
 *   3601/3600/3599mV        → test_boundary_3601_3600_3599          (§7.2 critical_mv)
 *   29.999/30s              → test_critical_hold_29999_30000        (§7.2 critical_hold_ms)
 *   短低压后恢复            → test_short_low_then_recover           (§7.2 连续定义+low_exit)
 *   反弹                    → test_rebound_after_critical_latched   (§7.2 锁存)
 *   缺测                    → test_sample_gap_over_2s               (§7.2 间隔≤2s)
 *   ADC错误                 → test_adc_fault_3_errors_controlled_sleep (§7.1 故障)
 *   低压冷启动（补充）      → test_boot_low_wake_radio_gate         (§7.3 BOOT_CHECK)
 *   recovery 稳定 10s（补充）→ test_recovery_3700_stable_10s        (§7.2 recovery_mv)
 *   LOW_WARN 迟滞 3750（补充）→ test_low_warn_hysteresis_3750       (§7.2 low_exit_mv)
 *   末帧超时 1s（补充）     → test_final_frame_timeout_1s           (§7.2 final_frame_timeout_ms)
 *   占位三态/校准注入/范围外 → test_placeholder_wireless_states (§7.3)、
 *                              test_calibration_offset_gain (§7.1 P4.4 预留)、
 *                              test_out_of_range_unknown (§7.1 2500–4500)
 * 任何 FAIL 退出码 1。
 */
#include <stdio.h>
#include <string.h>

#include "cdt_power.h"

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

/* ---------- 输入构造辅助 ---------- */

static cdt_power_params_t base_params(void)
{
    cdt_power_params_t p;
    cdt_power_params_init(&p);
    return p;
}

static void fsm_start(cdt_power_fsm_t *f, bool wake_low_hint)
{
    cdt_power_params_t p = base_params();
    cdt_power_init(f, &p, wake_low_hint);
}

static cdt_power_input_t smp(uint16_t mv, int64_t at)
{
    cdt_power_input_t in;
    in.kind = CDT_POWER_IN_SAMPLE;
    in.sample.battery_mv = mv;
    in.sample.valid = true;
    in.sample.at_ms = at;
    in.event = CDT_POWER_EVT_NONE;
    return in;
}

static cdt_power_input_t smp_inv(int64_t at)
{
    cdt_power_input_t in = smp(0, at);
    in.sample.valid = false;
    return in;
}

static cdt_power_input_t evt(cdt_power_event_t e)
{
    cdt_power_input_t in;
    in.kind = CDT_POWER_IN_EVENT;
    in.sample.battery_mv = 0;
    in.sample.valid = false;
    in.sample.at_ms = 0;
    in.event = e;
    return in;
}

static cdt_power_input_t tick(void)
{
    cdt_power_input_t in;
    in.kind = CDT_POWER_IN_NONE;
    in.sample.battery_mv = 0;
    in.sample.valid = false;
    in.sample.at_ms = 0;
    in.event = CDT_POWER_EVT_NONE;
    return in;
}

#define HAS(act, bit) (((act) & (bit)) != 0)

/* 健康启动捷径：4000mV 首样本 → ACTIVE + 允许开无线（§7.3 BOOT_CHECK）*/
static void boot_healthy(cdt_power_fsm_t *f)
{
    cdt_power_input_t in = smp(4000, 0);
    (void)cdt_power_step(f, &in, 0);
}

/* ---------- §8：3601/3600/3599mV 边界（§7.2 critical_mv=3600，≤ 计低压）---------- */

static void test_boundary_3601_3600_3599(void)
{
    cdt_power_fsm_t f1, f2;
    cdt_power_input_t in;
    cdt_power_action_t a;
    char ab[160];

    /* 3601：>3600 不计持续低压，但 ≤3700 进入 LOW_WARN（§7.3 LOW_WARN 行）*/
    fsm_start(&f1, false);
    in = smp(4000, 0);
    a = cdt_power_step(&f1, &in, 0);
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) && f1.state == CDT_POWER_ACTIVE,
          "健康启动 4000mV → ACTIVE+ALLOW_RADIO_START", NULL);

    in = smp(3601, 1000);
    a = cdt_power_step(&f1, &in, 1000);
    check(HAS(a, CDT_POWER_ACT_ENTER_LOW_WARN) && f1.state == CDT_POWER_LOW_WARN,
          "3601mV → 进入 LOW_WARN（≤3700，§7.3）", NULL);
    check(!f1.crit_running, "3601mV >3600 不开始 critical 计时（§7.2 ≤3600 才计）", NULL);

    in = tick();
    a = cdt_power_step(&f1, &in, 31500);
    check(!HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) && !f1.critical_latched &&
              f1.state == CDT_POWER_LOW_WARN,
          "3601mV 后 30.5s 仍不 CRITICAL（从未计时）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* 3600：恰好等于阈值，开始计时（≤3600）；3599：继续计时 */
    fsm_start(&f2, false);
    boot_healthy(&f2);
    in = smp(3600, 1000);
    (void)cdt_power_step(&f2, &in, 1000);
    check(f2.crit_running && f2.crit_start_ms == 1000 && f2.state == CDT_POWER_LOW_WARN,
          "3600mV（=阈值）→ critical 计时开始", NULL);
    in = smp(3599, 2000);
    (void)cdt_power_step(&f2, &in, 2000);
    check(f2.crit_running && f2.crit_start_ms == 1000,
          "3599mV（<阈值）→ 计时继续不清零", NULL);

    /* §7.2：任一有效样本 >3600 重置 critical 计时 */
    in = smp(4000, 3000);
    (void)cdt_power_step(&f2, &in, 3000);
    check(!f2.crit_running && !f2.critical_latched,
          "4000mV → critical 计时重置（§7.2 连续定义）", NULL);
}

/* ---------- §8：29.999/30s 临界持续（§7.2 critical_hold_ms=30000）---------- */

static void test_critical_hold_29999_30000(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;
    char ab[160];

    fsm_start(&f, false);
    boot_healthy(&f);
    /* 1Hz 连续有效 3600mV：t=1000..30000（首个低压样本 1000 起计时）*/
    for (t = 1000; t <= 30000; t += 1000) {
        in = smp(3600, t);
        a = cdt_power_step(&f, &in, t);
        check(!HAS(a, CDT_POWER_ACT_ENTER_CRITICAL), "30s 内 1Hz 低压样本不提前 CRITICAL",
              cdt_power_actions_str(ab, sizeof(ab), a));
    }
    check(f.state == CDT_POWER_LOW_WARN && !f.critical_latched,
          "持续 29s 仍 LOW_WARN 未锁存", NULL);

    in = tick();
    a = cdt_power_step(&f, &in, 30999);
    check(!HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) && !f.critical_latched,
          "29.999s（hold=29999ms）不锁存 CRITICAL（§7.2 单调时钟毫秒判定）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    in = tick();
    a = cdt_power_step(&f, &in, 31000);
    check(HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) && f.state == CDT_POWER_CRITICAL &&
              f.critical_latched,
          "30s（hold=30000ms）→ CRITICAL 锁存（§7.2）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    in = tick();
    a = cdt_power_step(&f, &in, 31001);
    check(HAS(a, CDT_POWER_ACT_BEGIN_SLEEP_PREP) && f.state == CDT_POWER_SLEEP_PREP,
          "锁存后下一步进入 SLEEP_PREP（§7.3 CRITICAL→SLEEP_PREP）",
          cdt_power_actions_str(ab, sizeof(ab), a));
}

/* ---------- §8：短低压后恢复（不进 CRITICAL；§7.2 low_exit 稳定 10s）---------- */

static void test_short_low_then_recover(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;

    fsm_start(&f, false);
    boot_healthy(&f);
    /* 5 秒短低压：t=1000..6000 @3600mV（hold 最长 5000 <30000）*/
    for (t = 1000; t <= 6000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    /* 4000mV：>3600 重置 critical；>3750 开始退出稳定计时（t=7000）*/
    in = smp(4000, 7000);
    (void)cdt_power_step(&f, &in, 7000);
    /* 1Hz 4000mV 直到 t=16000：稳定 9s，尚未解除 */
    for (t = 8000; t <= 16000; t += 1000) {
        in = smp(4000, t);
        (void)cdt_power_step(&f, &in, t);
    }
    check(f.state == CDT_POWER_LOW_WARN && f.lowexit_running && !f.critical_latched,
          "恢复 9s 时仍 LOW_WARN（稳定 10s 未满，§7.2）", NULL);

    in = smp(4000, 17000);
    a = cdt_power_step(&f, &in, 17000);
    check(HAS(a, CDT_POWER_ACT_EXIT_LOW_WARN) && f.state == CDT_POWER_ACTIVE,
          ">3750mV 连续稳定 10s → 退出 LOW_WARN 回 ACTIVE（§7.2）", NULL);
    check(!f.critical_latched && !f.crit_running,
          "短低压全程未成立 CRITICAL（hold 最长 5s <30s）", NULL);
}

/* ---------- §8：反弹（§7.2 锁存不撤销，仍走 SLEEP_PREP→DEEP_SLEEP）---------- */

static void test_rebound_after_critical_latched(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;
    char ab[160];

    fsm_start(&f, false);
    boot_healthy(&f);
    for (t = 1000; t <= 30000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    in = tick();
    a = cdt_power_step(&f, &in, 31000);
    check(HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) && f.critical_latched,
          "30s 连续低压成立 CRITICAL（前置）", cdt_power_actions_str(ab, sizeof(ab), a));

    /* 电压反弹到 4200mV：锁存不撤销 */
    in = smp(4200, 32000);
    a = cdt_power_step(&f, &in, 32000);
    check(HAS(a, CDT_POWER_ACT_BEGIN_SLEEP_PREP) && f.state == CDT_POWER_SLEEP_PREP,
          "反弹后仍进入 SLEEP_PREP（§7.2 锁存不因反弹撤销）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    check(f.critical_latched, "锁存标志保持 true", NULL);

    /* SLEEP_PREP 进入后 500ms（< 末帧 1s 预算）继续反弹：不解除、不回运行态 */
    in = smp(4200, 32500);
    a = cdt_power_step(&f, &in, 32500);
    check(f.critical_latched && f.state == CDT_POWER_SLEEP_PREP &&
              !HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) &&
              !HAS(a, CDT_POWER_ACT_EXIT_LOW_WARN),
          "持续反弹不解除、不回到运行态", cdt_power_actions_str(ab, sizeof(ab), a));

    in = evt(CDT_POWER_EVT_FINAL_FRAME_DONE);
    a = cdt_power_step(&f, &in, 32600);
    check(HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) && f.state == CDT_POWER_DEEP_SLEEP &&
              f.final_frame_done && !f.final_frame_timed_out,
          "反弹轨迹仍走完 SLEEP_PREP→DEEP_SLEEP（末帧完成，§7.3）",
          cdt_power_actions_str(ab, sizeof(ab), a));
}

/* ---------- §8：缺测（§7.2 连续有效样本间隔 ≤2s，缺测不计入持续低压）---------- */

static void test_sample_gap_over_2s(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;

    fsm_start(&f, false);
    boot_healthy(&f);
    /* 1Hz 3600mV：t=1000..29000（crit_start=1000）*/
    for (t = 1000; t <= 29000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    /* 缺测 11s 后恢复采样：间隔 11000ms >2000ms 上限 */
    in = smp(3600, 40000);
    a = cdt_power_step(&f, &in, 40000);
    check(HAS(a, CDT_POWER_ACT_SAMPLE_GAP_RESET),
          "间隔 >2s → SAMPLE_GAP_RESET 转采样故障检查（§7.2）", NULL);
    check(f.crit_start_ms == 40000 && f.gap_reset_count == 1,
          "critical 计时自缺测后首个样本重新起算（缺测不计入）", NULL);
    check(!f.critical_latched && !f.battery_fault && f.invalid_streak == 0,
          "缺测本身不计失败样本、不锁存（§7.1 故障=连续失败读数）", NULL);

    /* 恢复后重新计满 30s 才锁存：t=41000..69000 时 hold=29000 */
    for (t = 41000; t <= 69000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    check(!f.critical_latched && f.state == CDT_POWER_LOW_WARN,
          "若无缺测重置本应于 31s 锁存；实际 29s 处仍未锁存", NULL);

    in = smp(3600, 70000);
    a = cdt_power_step(&f, &in, 70000);
    check(HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) && f.critical_latched,
          "缺测后重新连续 30s → CRITICAL（§7.2）", NULL);
}

/* ---------- §8：ADC错误（§7.1 连续 3 次失败→BATTERY_FAULT→10s→受控休眠）---------- */

static void test_adc_fault_3_errors_controlled_sleep(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    char ab[160];

    /* 主轨迹：3 连错 → 故障 → 10s 宽限 → 受控休眠 */
    fsm_start(&f, false);
    boot_healthy(&f);
    in = smp_inv(1000);
    a = cdt_power_step(&f, &in, 1000);
    check(!f.battery_fault && a == CDT_POWER_ACT_NONE, "第 1 次无效样本不触发故障", NULL);
    in = smp_inv(2000);
    a = cdt_power_step(&f, &in, 2000);
    check(!f.battery_fault, "第 2 次无效样本不触发故障", NULL);
    in = smp_inv(3000);
    a = cdt_power_step(&f, &in, 3000);
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT) && f.battery_fault &&
              f.fault_since_ms == 3000,
          "连续第 3 次失败 → BATTERY_FAULT（关闭高功耗提示，§7.1）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    check(f.state == CDT_POWER_ACTIVE,
          "故障非独立状态：状态保持，动作交 adapter 执行（枚举缺口已报告 A0）", NULL);

    in = tick();
    a = cdt_power_step(&f, &in, 12999);
    check(!HAS(a, CDT_POWER_ACT_CONTROLLED_SLEEP),
          "故障 9.999s 仍不休眠（宽限 10s 未满，§7.1）", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 13000);
    check(HAS(a, CDT_POWER_ACT_CONTROLLED_SLEEP) && HAS(a, CDT_POWER_ACT_ENTER_CRITICAL) &&
              f.state == CDT_POWER_CRITICAL && f.critical_latched,
          "10s 不可恢复 → 受控休眠经 CRITICAL 故障安全路径（§7.1/§7.3）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    in = tick();
    a = cdt_power_step(&f, &in, 13001);
    check(HAS(a, CDT_POWER_ACT_BEGIN_SLEEP_PREP) && f.state == CDT_POWER_SLEEP_PREP,
          "故障页 SLEEP_PREP（caller 依 battery_fault 选故障页）", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 14001);
    check(HAS(a, CDT_POWER_ACT_FINAL_FRAME_TIMEOUT) && HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) &&
              f.state == CDT_POWER_DEEP_SLEEP,
          "末帧超时仍进 DEEP_SLEEP（§7.2 final_frame_timeout_ms）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* 反例 A：2 连错 + 有效样本 → 不成立故障（§7.1"连续"）*/
    fsm_start(&f, false);
    boot_healthy(&f);
    in = smp_inv(1000);
    (void)cdt_power_step(&f, &in, 1000);
    in = smp_inv(2000);
    (void)cdt_power_step(&f, &in, 2000);
    in = smp(4000, 3000);
    a = cdt_power_step(&f, &in, 3000);
    check(!f.battery_fault && f.invalid_streak == 0 && !HAS(a, CDT_POWER_ACT_BATTERY_FAULT),
          "有效样本打断连续失败计数（§7.1）", NULL);

    /* 反例 B：宽限期内有效样本 → 故障恢复 */
    fsm_start(&f, false);
    boot_healthy(&f);
    in = smp_inv(1000);
    (void)cdt_power_step(&f, &in, 1000);
    in = smp_inv(2000);
    (void)cdt_power_step(&f, &in, 2000);
    in = smp_inv(3000);
    (void)cdt_power_step(&f, &in, 3000);
    in = smp(4000, 5000);
    a = cdt_power_step(&f, &in, 5000);
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT_RECOVERED) && !f.battery_fault,
          "10s 内有效样本 → BATTERY_FAULT 恢复（§7.1）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    in = tick();
    a = cdt_power_step(&f, &in, 15000);
    check(!HAS(a, CDT_POWER_ACT_CONTROLLED_SLEEP) && f.state == CDT_POWER_ACTIVE,
          "恢复后不再触发受控休眠", cdt_power_actions_str(ab, sizeof(ab), a));
}

/* ---------- 补充：低压冷启动 BOOT_CHECK 拒绝开无线（§7.3；HARDWARE §4 保底）---------- */

static void test_boot_low_wake_radio_gate(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;
    bool radio_allowed;
    char ab[160];

    /* A：RTC 低压原因（hint）+ 健康电压：仍要求 recovery，不开无线直到稳定 */
    fsm_start(&f, true);
    in = smp(4100, 0);
    a = cdt_power_step(&f, &in, 0);
    check(HAS(a, CDT_POWER_ACT_BLOCK_RADIO_START) && !HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) &&
              f.state == CDT_POWER_BOOT_CHECK && f.boot_recovery_required,
          "hint 低压唤醒：即使 4100mV 也先不开无线（§7.3 先 ADC 后无线）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    for (t = 1000; t <= 9000; t += 1000) {
        in = smp(4100, t);
        (void)cdt_power_step(&f, &in, t);
    }
    in = tick();
    a = cdt_power_step(&f, &in, 9999);
    check(f.state == CDT_POWER_BOOT_CHECK, "recovery 稳定 9.999s 仍在 BOOT_CHECK", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 10000);
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) && f.state == CDT_POWER_ACTIVE,
          "recovery 通过才 ACTIVE 并允许开无线（§7.2/§7.3）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* B：无 hint，首次样本即 3600（≤3600）→ 不开无线直接回 SLEEP_PREP（§7.3）*/
    fsm_start(&f, false);
    radio_allowed = false;
    in = smp(3600, 0);
    a = cdt_power_step(&f, &in, 0);
    radio_allowed = HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START);
    check(HAS(a, CDT_POWER_ACT_BLOCK_RADIO_START) && f.state == CDT_POWER_SLEEP_PREP &&
              f.critical_latched && !radio_allowed,
          "首样本 ≤3600：拒绝开无线，直接 SLEEP_PREP 且锁存（§7.3）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    in = tick();
    a = cdt_power_step(&f, &in, 999);
    check(f.state == CDT_POWER_SLEEP_PREP, "末帧等待 999ms 仍 SLEEP_PREP", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 1000);
    radio_allowed = radio_allowed || HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START);
    check(HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) && f.state == CDT_POWER_DEEP_SLEEP &&
              !radio_allowed,
          "低压冷启动全流程从未允许无线，最终 DEEP_SLEEP（§7.3；P5.3 唤醒门禁）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* C：无 hint + 首样本健康 → 正常 ACTIVE（§7.3"正常启动健康电压可运行"）*/
    fsm_start(&f, false);
    in = smp(4000, 0);
    a = cdt_power_step(&f, &in, 0);
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) && f.state == CDT_POWER_ACTIVE &&
              !HAS(a, CDT_POWER_ACT_BLOCK_RADIO_START),
          "正常健康电压直接 ACTIVE", cdt_power_actions_str(ab, sizeof(ab), a));
}

/* ---------- 补充：recovery 3700 稳定 10s 恢复（§7.2 recovery_mv，≥ 含边界）---------- */

static void test_recovery_3700_stable_10s(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    char ab[160];

    fsm_start(&f, true);
    in = smp(3700, 0);
    a = cdt_power_step(&f, &in, 0);
    check(HAS(a, CDT_POWER_ACT_BLOCK_RADIO_START) && f.recov_running &&
              f.recov_start_ms == 0,
          "3700mV（=recovery_mv）计入恢复计时（≥3700 含边界，§7.2）", NULL);
    in = smp(3699, 1000);
    (void)cdt_power_step(&f, &in, 1000);
    check(!f.recov_running, "3699mV（<3700）重置恢复计时", NULL);
    in = smp(3700, 2000);
    (void)cdt_power_step(&f, &in, 2000);
    check(f.recov_running && f.recov_start_ms == 2000, "回到 3700 重新起算", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 11999);
    check(f.state == CDT_POWER_BOOT_CHECK, "稳定 9.999s 未恢复", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 12000);
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) && f.state == CDT_POWER_ACTIVE &&
              !f.boot_recovery_required,
          "≥3700 连续稳定 10s → ACTIVE+允许无线（§7.2 recovery_mv）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* 迟滞带 3601–3699：不放弃也不计时，保持 BOOT_CHECK */
    fsm_start(&f, true);
    in = smp(3700, 0);
    (void)cdt_power_step(&f, &in, 0);
    in = smp(3650, 1000);
    (void)cdt_power_step(&f, &in, 1000);
    check(f.state == CDT_POWER_BOOT_CHECK && !f.recov_running && !f.critical_latched,
          "3650mV 在迟滞带：等待，不误判放弃也不计时", NULL);
}

/* ---------- 补充：LOW_WARN 迟滞 3750 边界（§7.2 low_exit_mv 严格大于）---------- */

static void test_low_warn_hysteresis_3750(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;

    fsm_start(&f, false);
    boot_healthy(&f);
    in = smp(3700, 1000);
    a = cdt_power_step(&f, &in, 1000);
    check(HAS(a, CDT_POWER_ACT_ENTER_LOW_WARN),
          "3700mV（=low_enter_mv，≤）进入 LOW_WARN", NULL);

    in = smp(3750, 2000);
    (void)cdt_power_step(&f, &in, 2000);
    check(!f.lowexit_running, "3750mV 不解除（退出需严格 >3750，§7.2）", NULL);

    in = smp(3751, 3000);
    (void)cdt_power_step(&f, &in, 3000);
    check(f.lowexit_running && f.lowexit_start_ms == 3000, "3751mV 开始退出稳定计时", NULL);

    in = smp(3750, 4000);
    (void)cdt_power_step(&f, &in, 4000);
    check(!f.lowexit_running, "计时中一次 ≤3750 即重置（连续稳定）", NULL);

    in = smp(3751, 5000);
    (void)cdt_power_step(&f, &in, 5000);
    check(f.lowexit_running && f.lowexit_start_ms == 5000, "重新起算 5000", NULL);

    in = tick();
    a = cdt_power_step(&f, &in, 14000);
    check(f.state == CDT_POWER_LOW_WARN, "稳定 9s 未解除", NULL);
    in = tick();
    a = cdt_power_step(&f, &in, 15000);
    check(HAS(a, CDT_POWER_ACT_EXIT_LOW_WARN) && f.state == CDT_POWER_ACTIVE,
          "3751 连续稳定 10s → 退出警告（§7.2 low_exit_mv）", NULL);
}

/* ---------- 补充：末帧超时 1s 仍进 DEEP_SLEEP（§7.2 final_frame_timeout_ms）---------- */

static void test_final_frame_timeout_1s(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;
    int64_t t;
    char ab[160];

    /* A：超时路径 */
    fsm_start(&f, false);
    boot_healthy(&f);
    for (t = 1000; t <= 30000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    in = tick();
    (void)cdt_power_step(&f, &in, 31000); /* CRITICAL 锁存 */
    in = tick();
    (void)cdt_power_step(&f, &in, 31001); /* SLEEP_PREP，since=31001 */

    in = tick();
    a = cdt_power_step(&f, &in, 32000);
    check(f.state == CDT_POWER_SLEEP_PREP && !HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY),
          "末帧等待 999ms 继续等待（§7.2 最多 1s）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    in = tick();
    a = cdt_power_step(&f, &in, 32001);
    check(HAS(a, CDT_POWER_ACT_FINAL_FRAME_TIMEOUT) && HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) &&
              f.state == CDT_POWER_DEEP_SLEEP && f.final_frame_timed_out,
          "末帧 1s 超时也必须休眠（§7.2/§7.3 显示失败也可睡眠）",
          cdt_power_actions_str(ab, sizeof(ab), a));
    in = smp(4200, 33000);
    a = cdt_power_step(&f, &in, 33000);
    in = evt(CDT_POWER_EVT_ACTIVITY);
    a = cdt_power_step(&f, &in, 33100);
    check(f.state == CDT_POWER_DEEP_SLEEP && a == CDT_POWER_ACT_NONE,
          "DEEP_SLEEP 终态忽略一切输入（唤醒=PWR 重新上电，HARDWARE §4）",
          cdt_power_actions_str(ab, sizeof(ab), a));

    /* B：及时 flush 路径（≤1s 内完成）*/
    fsm_start(&f, false);
    boot_healthy(&f);
    for (t = 1000; t <= 30000; t += 1000) {
        in = smp(3600, t);
        (void)cdt_power_step(&f, &in, t);
    }
    in = tick();
    (void)cdt_power_step(&f, &in, 31000);
    in = evt(CDT_POWER_EVT_FINAL_FRAME_DONE);
    a = cdt_power_step(&f, &in, 31100);
    check(HAS(a, CDT_POWER_ACT_BEGIN_SLEEP_PREP) && HAS(a, CDT_POWER_ACT_DEEP_SLEEP_READY) &&
              f.state == CDT_POWER_DEEP_SLEEP && f.final_frame_done &&
              !f.final_frame_timed_out,
          "CRITICAL 后 flush 即时完成 → 一步直达 DEEP_SLEEP",
          cdt_power_actions_str(ab, sizeof(ab), a));
}

/* ---------- 补充：ACTIVE↔CONNECTED_IDLE↔OFFLINE_LIGHT_SLEEP 占位（§7.3，P5.2 实测）---------- */

static void test_placeholder_wireless_states(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;

    fsm_start(&f, false);
    boot_healthy(&f);
    in = evt(CDT_POWER_EVT_IDLE_REACHED);
    a = cdt_power_step(&f, &in, 1000);
    check(HAS(a, CDT_POWER_ACT_ENTER_CONNECTED_IDLE) && f.state == CDT_POWER_CONNECTED_IDLE,
          "无脏画面占位 → CONNECTED_IDLE（P5.2 定策略）", NULL);
    in = evt(CDT_POWER_EVT_ACTIVITY);
    a = cdt_power_step(&f, &in, 2000);
    check(HAS(a, CDT_POWER_ACT_ENTER_ACTIVE) && f.state == CDT_POWER_ACTIVE,
          "活动/新状态到达 → ACTIVE", NULL);
    in = evt(CDT_POWER_EVT_OFFLINE_SLEEP);
    a = cdt_power_step(&f, &in, 3000);
    check(HAS(a, CDT_POWER_ACT_ENTER_OFFLINE_LIGHT_SLEEP) &&
              f.state == CDT_POWER_OFFLINE_LIGHT_SLEEP,
          "主动离线占位 → OFFLINE_LIGHT_SLEEP（P5.2 定策略）", NULL);
    in = evt(CDT_POWER_EVT_KEY_WAKE);
    a = cdt_power_step(&f, &in, 4000);
    check(HAS(a, CDT_POWER_ACT_ENTER_ACTIVE) && f.state == CDT_POWER_ACTIVE,
          "KEY 唤醒 → ACTIVE（重连拿全量占位）", NULL);

    /* CONNECTED_IDLE 中低压 → LOW_WARN；警告态中活动不改电池域状态 */
    in = evt(CDT_POWER_EVT_IDLE_REACHED);
    (void)cdt_power_step(&f, &in, 4500);
    in = smp(3700, 5000);
    a = cdt_power_step(&f, &in, 5000);
    check(HAS(a, CDT_POWER_ACT_ENTER_LOW_WARN) && f.state == CDT_POWER_LOW_WARN,
          "CONNECTED_IDLE 中 ≤3700 → LOW_WARN", NULL);
    in = evt(CDT_POWER_EVT_ACTIVITY);
    a = cdt_power_step(&f, &in, 6000);
    check(f.state == CDT_POWER_LOW_WARN && !HAS(a, CDT_POWER_ACT_ENTER_ACTIVE),
          "LOW_WARN 中业务继续但不离开警告态（§7.3 LOW_WARN 行）", NULL);
}

/* ---------- 补充：校准注入 offset/gain（§7.1 公式，P4.4 预留）---------- */

static void test_calibration_offset_gain(void)
{
    cdt_power_fsm_t f;
    cdt_power_params_t p;
    cdt_power_input_t in;

    /* 恒等（默认）：3600 开始计时 */
    p = base_params();
    cdt_power_init(&f, &p, false);
    boot_healthy(&f);
    in = smp(3600, 1000);
    (void)cdt_power_step(&f, &in, 1000);
    check(f.crit_running, "默认校准恒等：3600 计入低压", NULL);

    /* offset +40mV：3600 → 3640，不计时但仍 ≤3700 进警告 */
    p = base_params();
    p.cal_offset_mv = 40;
    cdt_power_init(&f, &p, false);
    boot_healthy(&f);
    in = smp(3600, 1000);
    (void)cdt_power_step(&f, &in, 1000);
    check(!f.crit_running && f.state == CDT_POWER_LOW_WARN,
          "offset=+40：3600→3640 不计时、进 LOW_WARN（§7.1 注入可校准）", NULL);

    /* gain 0.998（998000ppm）：3601 → 3593，开始计时（原始值 3601 本不会）*/
    p = base_params();
    p.cal_gain_ppm = 998000;
    cdt_power_init(&f, &p, false);
    boot_healthy(&f);
    in = smp(3601, 1000);
    (void)cdt_power_step(&f, &in, 1000);
    check(f.crit_running && cdt_power_effective_mv(&p, 3601) == 3593,
          "gain=0.998：3601→3593 计入低压（整数 ppm，无浮点）", NULL);
}

/* ---------- 补充：范围外 → unknown 计失败（§7.1 2500–4500）---------- */

static void test_out_of_range_unknown(void)
{
    cdt_power_fsm_t f;
    cdt_power_input_t in;
    cdt_power_action_t a;

    fsm_start(&f, false);
    boot_healthy(&f);
    in = smp(2000, 1000); /* valid=true 但 <2500 → unknown */
    a = cdt_power_step(&f, &in, 1000);
    check(a == CDT_POWER_ACT_NONE && !f.crit_running && f.invalid_streak == 1,
          "2000mV 范围外按失败样本计（§7.1）", NULL);
    in = smp(5000, 2000); /* >4500 */
    a = cdt_power_step(&f, &in, 2000);
    check(a == CDT_POWER_ACT_NONE && f.invalid_streak == 2, "5000mV 计第 2 次失败", NULL);
    in = smp(2000, 3000);
    a = cdt_power_step(&f, &in, 3000);
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT),
          "范围外连续 3 次 → BATTERY_FAULT（§7.1 范围外/驱动失败同路径）", NULL);
}

int main(void)
{
    test_boundary_3601_3600_3599();
    test_critical_hold_29999_30000();
    test_short_low_then_recover();
    test_rebound_after_critical_latched();
    test_sample_gap_over_2s();
    test_adc_fault_3_errors_controlled_sleep();
    test_boot_low_wake_radio_gate();
    test_recovery_3700_stable_10s();
    test_low_warn_hysteresis_3750();
    test_final_frame_timeout_1s();
    test_placeholder_wireless_states();
    test_calibration_offset_gain();
    test_out_of_range_unknown();
    printf("\n汇总: %d PASS, %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
