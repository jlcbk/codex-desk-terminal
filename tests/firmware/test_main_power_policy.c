/*
 * test_main_power_policy.c — main 电源编排策略模型测试（审核 R3a/R3b 修复验收）
 *
 * 审核真源：docs/ARCHITECTURE_REVIEW_2026-09-11.md R3a/R3b（宿主复现思路
 * codex-review-power-probe.c：真实 shared/power FSM + main 调用策略模型）。
 * 策略判定单源：firmware/main/app_power_policy.h（main.c 与本测试同时 include，
 * 保证钉住的就是固件在用的睡眠掩码/失败样本构造，两处不漂移）。
 *
 * 复演审核复现场景（修复后断言）：
 *   R3a 冷启动 3590mV：审核输出 "cold boot t=1000 state=DEEP_SLEEP actions=0x30
 *   dispatch_sleep=0"（BEGIN_SLEEP_PREP 被丢弃）→ 修复后断言睡眠执行器恰被
 *   调用一次且不开无线（DEVELOPMENT_PLAN §7.3 BOOT_CHECK 行）。
 *   R3b ADC 整批失败：审核输出 "ADC full failure 60s: state=ACTIVE
 *   invalid_streak=0"（旧 valid 样本被反复复用）→ 修复后断言 invalid 样本
 *   （当前时间）逐次进 FSM、invalid_streak 推进到 3 触发 BATTERY_FAULT、
 *   Runtime 电压位转 unknown（"--"）、宽限期后受控休眠真实执行。
 *
 * 边界：纯宿主；睡眠执行器（= main do_sleep_sequence，真机不返回）以桩计数
 * 替代——断言「睡眠执行器被真实调用」，不是板卡电流实测（真机项归下一轮）。
 */
#include <stdio.h>
#include <string.h>

#include "app_power_policy.h"

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

#define HAS(act, bit) (((act) & (bit)) != 0)

/* ------------------------------------------------------------------ */
/* main 策略模型（镜像 firmware/main/main.c 的调用语义，2026-09-11 修复版） */
/* ------------------------------------------------------------------ */

/* 睡眠执行器桩（= main 的 do_sleep_sequence；真机不返回，模型返回继续演化） */
static int g_sleep_exec_calls;
/* main 的 s_sleep_latched（R3a：power_handle_actions 置位，调用点执行） */
static int g_sleep_latched;
/* main 的 Runtime 电压镜像（runtime_update 门：s_have_sample && valid） */
static cdt_power_sample_t g_last_sample;
static int g_runtime_battery_valid;

static void model_reset(void)
{
    g_sleep_exec_calls = 0;
    g_sleep_latched = 0;
    memset(&g_last_sample, 0, sizeof g_last_sample);
    g_runtime_battery_valid = -1;
}

static void model_runtime_update(void)
{
    /* main.c runtime_update：invalid → battery_valid=false → presenter "--" */
    g_runtime_battery_valid = g_last_sample.valid ? 1 : 0;
}

/* 采样桩：err==0 视为 ESP_OK（整批成功，valid/mv 生效）；err!=0 视为驱动级
 * 整批失败（cdt_battery_sample_batch 返回非 ESP_OK，样本不可用）。 */
typedef struct {
    int err;
    uint16_t mv;
    int valid;
} battery_stub_t;

/* main.c 采样喂入（boot_check 与主循环共用形态；R3b 失败分支 = invalid@now） */
static cdt_power_sample_t model_take_sample(int64_t now, const battery_stub_t *bat)
{
    cdt_power_sample_t s;
    if (bat->err == 0) {
        s.battery_mv = bat->mv;
        s.valid = bat->valid != 0;
    } else {
        s = app_power_invalid_sample(now); /* R3b：绝不复用旧样本 */
    }
    s.at_ms = now;
    return s;
}

/* main.c power_handle_actions（R3a 掩码 → 锁存）+ 调用点执行 */
static void model_dispatch(cdt_power_action_t acts)
{
    if (app_power_sleep_requested(acts)) {
        g_sleep_latched = 1; /* s_sleep_latched = true */
    }
    if (g_sleep_latched) {
        g_sleep_exec_calls++; /* do_sleep_sequence(acts)；真机不返回 */
        g_sleep_latched = 0;  /* 模型继续演化（锁存语义由恰一次断言钉住） */
    }
}

/* main.c boot_check 策略模型 */
static cdt_power_action_t model_boot_check(cdt_power_fsm_t *fsm, int64_t now,
                                           const battery_stub_t *bat,
                                           int *radio_allowed)
{
    g_last_sample = model_take_sample(now, bat);
    cdt_power_input_t in = {
        .kind = CDT_POWER_IN_SAMPLE,
        .sample = g_last_sample,
        .event = 0,
    };
    cdt_power_action_t acts = cdt_power_step(fsm, &in, now);
    model_dispatch(acts);
    model_runtime_update();
    *radio_allowed = HAS(acts, CDT_POWER_ACT_ALLOW_RADIO_START) && !g_sleep_latched;
    return acts;
}

/* main.c 主循环电池采样分支策略模型 */
static cdt_power_action_t model_loop_sample(cdt_power_fsm_t *fsm, int64_t now,
                                            const battery_stub_t *bat)
{
    g_last_sample = model_take_sample(now, bat);
    cdt_power_input_t in = {
        .kind = CDT_POWER_IN_SAMPLE,
        .sample = g_last_sample,
        .event = 0,
    };
    cdt_power_action_t acts = cdt_power_step(fsm, &in, now);
    model_dispatch(acts);
    model_runtime_update();
    return acts;
}

static cdt_power_input_t smp(uint16_t mv, int64_t at)
{
    cdt_power_input_t in;
    in.kind = CDT_POWER_IN_SAMPLE;
    in.sample.battery_mv = mv;
    in.sample.valid = true;
    in.sample.at_ms = at;
    in.event = 0;
    return in;
}

static void fsm_healthy_boot(cdt_power_fsm_t *f)
{
    cdt_power_input_t in = smp(4000, 0);
    cdt_power_params_t p;
    cdt_power_params_init(&p);
    cdt_power_init(f, &p, false);
    (void)cdt_power_step(f, &in, 0); /* 健康启动 → ACTIVE */
    g_last_sample.battery_mv = 4000; /* 同步 main 的 Runtime 镜像 */
    g_last_sample.valid = 1;
    g_last_sample.at_ms = 0;
    model_runtime_update();
}

/* ---------- R3a：低压冷启动丢失一次性睡眠动作 ---------- */

/* 审核复现场景：低压唤醒（RTC hint），首次 3590mV ≤ critical_mv=3600，
 * recovery 不满足 → §7.3 BOOT_CHECK：不开无线直接回 SLEEP_PREP。 */
static void test_r3a_boot_low_wake_sleeps_exactly_once(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    int radio = -1;
    cdt_power_action_t acts;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, true); /* RTC 低压唤醒 hint */

    acts = model_boot_check(&fsm, 1000, &(battery_stub_t){ 0, 3590, 1 }, &radio);

    check(HAS(acts, CDT_POWER_ACT_BEGIN_SLEEP_PREP),
          "r3a/boot_low_wake: FSM 返回 BEGIN_SLEEP_PREP",
          "actions 未含 BEGIN_SLEEP_PREP");
    check(fsm.state == CDT_POWER_SLEEP_PREP,
          "r3a/boot_low_wake: 状态进入 SLEEP_PREP",
          cdt_power_state_str(fsm.state));
    check(g_sleep_exec_calls == 1,
          "r3a/boot_low_wake: 睡眠执行器恰执行一次（审核 dispatch_sleep=0 → 1）",
          "sleep_exec_calls != 1");
    check(radio == 0,
          "r3a/boot_low_wake: 不开无线（§7.3 低压唤醒门禁）",
          "radio_allowed != 0");
}

/* 运行期持续低压：30s 锁存 CRITICAL → main 策略本拍锁存执行睡眠恰一次
 * （ENTER_CRITICAL ∈ 睡眠掩码；末帧渲染 critical/LOW BATTERY 页后深睡，
 * §7.3 critical 成立后 2s 内——原 main 运行时掩码本就含 ENTER_CRITICAL，
 * 本测试钉「真实执行」而非仅日志）。 */
static void test_r3a_runtime_low_sustained_sleeps_once(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    int i;
    cdt_power_action_t a;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    fsm_healthy_boot(&fsm); /* 4000mV@0 → ACTIVE（先有健康基线，审核场景） */

    /* §7.2：≤3600mV 连续 1Hz 快采；crit_start=1000，30s 到期在 t=31000 */
    for (i = 1; i <= 30; i++) {
        (void)model_loop_sample(&fsm, (int64_t)i * 1000,
                                &(battery_stub_t){ 0, 3590, 1 });
    }
    check(g_sleep_exec_calls == 0 && fsm.state == CDT_POWER_LOW_WARN,
          "r3a/runtime: 30s 前不触发睡眠（LOW_WARN 业务继续）",
          cdt_power_state_str(fsm.state));

    a = model_loop_sample(&fsm, 31000, &(battery_stub_t){ 0, 3590, 1 });
    check(HAS(a, CDT_POWER_ACT_ENTER_CRITICAL),
          "r3a/runtime: 30s 连续低压 → ENTER_CRITICAL", "无 ENTER_CRITICAL");
    check(g_sleep_exec_calls == 1 && fsm.state == CDT_POWER_CRITICAL,
          "r3a/runtime: ENTER_CRITICAL 拍锁存并真实执行睡眠恰一次",
          "calls != 1 或未锁存");
}

/* 掩码单源断言：DEEP_SLEEP_READY（审核：cdt_power.c check_timeouts 末帧超时
 * 返回该动作，旧 dispatcher 不处理）必须触发睡眠；非睡眠动作不得误触发。 */
static void test_r3a_mask_covers_deep_sleep_ready(void)
{
    check(app_power_sleep_requested(CDT_POWER_ACT_DEEP_SLEEP_READY),
          "r3a/mask: DEEP_SLEEP_READY 触发睡眠", "掩码未覆盖");
    check(app_power_sleep_requested(CDT_POWER_ACT_ENTER_CRITICAL),
          "r3a/mask: ENTER_CRITICAL 触发睡眠", "掩码未覆盖");
    check(app_power_sleep_requested(CDT_POWER_ACT_BEGIN_SLEEP_PREP),
          "r3a/mask: BEGIN_SLEEP_PREP 触发睡眠", "掩码未覆盖");
    check(app_power_sleep_requested(CDT_POWER_ACT_CONTROLLED_SLEEP),
          "r3a/mask: CONTROLLED_SLEEP 触发睡眠", "掩码未覆盖");
    check(!app_power_sleep_requested(CDT_POWER_ACT_NONE) &&
          !app_power_sleep_requested(CDT_POWER_ACT_ALLOW_RADIO_START) &&
          !app_power_sleep_requested(CDT_POWER_ACT_BATTERY_FAULT) &&
          !app_power_sleep_requested(CDT_POWER_ACT_ENTER_LOW_WARN),
          "r3a/mask: 非睡眠动作不误触发", "掩码过宽");
}

/* ---------- R3b：ADC 整批失败反复上报旧健康电压 ---------- */

/* 审核复现场景：先读到 4000mV，此后每批 ADC 全失败（模型以 1Hz 故障窗节奏
 * 喂入；节奏属调度层，受钉语义 = invalid 逐次进 FSM → 3 错 → 宽限 → 睡眠）。
 * 修复前：FSM 恒收旧 valid=true，invalid_streak=0，60s 后仍在 ACTIVE。
 * 修复后：3 错 BATTERY_FAULT → 10s 宽限不可恢复 → 受控休眠真实执行一次。 */
static void test_r3b_adc_full_failure_fault_then_sleep(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    int64_t t;
    int saw_fault = 0;
    int saw_controlled = 0;
    int fault_at_streak = -1;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    fsm_healthy_boot(&fsm);
    model_runtime_update();
    check(g_runtime_battery_valid == 1,
          "r3b/full_fail: 启动 4000mV → runtime 电压有效", "启动即无效");

    battery_stub_t fail = { -1 /* 非 ESP_OK：整批失败 */, 0, 0 };
    for (t = 1000; t <= 60000; t += 1000) {
        cdt_power_action_t a = model_loop_sample(&fsm, t, &fail);
        if (HAS(a, CDT_POWER_ACT_BATTERY_FAULT)) {
            saw_fault = 1;
            fault_at_streak = fsm.invalid_streak;
        }
        if (HAS(a, CDT_POWER_ACT_CONTROLLED_SLEEP)) {
            saw_controlled = 1;
        }
        if (g_sleep_exec_calls > 0) {
            break; /* 真机 do_sleep_sequence 不返回 */
        }
    }

    check(saw_fault == 1 && fault_at_streak == 3,
          "r3b/full_fail: 连续 3 错触发 BATTERY_FAULT（审核：streak 恒 0）",
          "未触发或错拍");
    check(g_runtime_battery_valid == 0,
          "r3b/full_fail: 失败期 Runtime 电压位 unknown（\"--\"）",
          "runtime 仍显示旧健康电压");
    check(saw_controlled == 1,
          "r3b/full_fail: 宽限 10s 不可恢复 → CONTROLLED_SLEEP", "未受控休眠");
    check(g_sleep_exec_calls == 1,
          "r3b/full_fail: 受控休眠真实执行恰一次（非仅日志）", "calls != 1");
    check(t <= 22000,
          "r3b/full_fail: 3 错 + 宽限 10s 内进入睡眠（远早于审核 60s 仍 ACTIVE）",
          "睡眠过晚");
}

/* 失败未达 3 错即恢复：invalid_streak 清零、Runtime 恢复显示、不睡眠 */
static void test_r3b_recover_on_valid_sample(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    cdt_power_action_t a;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    fsm_healthy_boot(&fsm);

    (void)model_loop_sample(&fsm, 10000, &(battery_stub_t){ -1, 0, 0 });
    (void)model_loop_sample(&fsm, 11000, &(battery_stub_t){ -1, 0, 0 });
    check(fsm.invalid_streak == 2 && g_runtime_battery_valid == 0,
          "r3b/recover: 2 错未触发故障且 Runtime unknown", "streak/显示错误");

    a = model_loop_sample(&fsm, 12000, &(battery_stub_t){ 0, 4100, 1 });
    check(fsm.invalid_streak == 0,
          "r3b/recover: 有效样本清零 invalid_streak", "streak 未清零");
    check(g_runtime_battery_valid == 1,
          "r3b/recover: Runtime 恢复显示电压", "未恢复");
    check(g_sleep_exec_calls == 0 && !HAS(a, CDT_POWER_ACT_BATTERY_FAULT),
          "r3b/recover: 2 错恢复不触发故障/睡眠", "误触发");
}

/* 启动首批失败：invalid@now 进 FSM（BOOT_CHECK 故障域计 1 错），不放行无线；
 * 随后有效样本恢复 → FSM 发 ALLOW_RADIO_START（审核 R3b 注：运行期该动作
 * 目前仅日志、不实际 start_radio，属已知剩余项，本测试钉 FSM 侧语义）。 */
static void test_r3b_boot_fail_then_fsm_recovers(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    int radio = -1;
    cdt_power_action_t acts;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);

    acts = model_boot_check(&fsm, 777, &(battery_stub_t){ -1, 0, 0 }, &radio);
    check(!HAS(acts, CDT_POWER_ACT_ALLOW_RADIO_START) && radio == 0,
          "r3b/boot_fail: 首批失败不放行无线", "误放行");
    check(g_last_sample.valid == 0 && g_last_sample.at_ms == 777,
          "r3b/boot_fail: 首批失败样本 = invalid@当前时间", "样本形态错误");
    check(fsm.invalid_streak == 1,
          "r3b/boot_fail: BOOT_CHECK 域内计 1 错（§7.1 故障域）", "未计错");

    /* 主循环下一批恢复（FSM 仍在 BOOT_CHECK，未决定启动判定）→ ALLOW */
    acts = model_loop_sample(&fsm, 1777, &(battery_stub_t){ 0, 4000, 1 });
    check(HAS(acts, CDT_POWER_ACT_ALLOW_RADIO_START),
          "r3b/boot_fail: 恢复后 FSM 发 ALLOW_RADIO_START", "未放行");
    check(g_sleep_exec_calls == 0,
          "r3b/boot_fail: 恢复路径不触发睡眠", "误睡眠");
}

/* R3b 样本构造单源：invalid 形态必须带当前时间戳（审核：旧样本时间戳不得复用）*/
static void test_r3b_invalid_sample_shape(void)
{
    cdt_power_sample_t s = app_power_invalid_sample(123456);
    check(s.valid == 0 && s.battery_mv == 0 && s.at_ms == 123456,
          "r3b/shape: invalid 样本 = {mv=0, valid=false, at=now}", "形态错误");
}

int main(void)
{
    test_r3a_boot_low_wake_sleeps_exactly_once();
    test_r3a_runtime_low_sustained_sleeps_once();
    test_r3a_mask_covers_deep_sleep_ready();
    test_r3b_adc_full_failure_fault_then_sleep();
    test_r3b_recover_on_valid_sample();
    test_r3b_boot_fail_then_fsm_recovers();
    test_r3b_invalid_sample_shape();

    printf("\n%d checks: %d passed, %d failed\n", passes + failures, passes,
           failures);
    return failures == 0 ? 0 : 1;
}
