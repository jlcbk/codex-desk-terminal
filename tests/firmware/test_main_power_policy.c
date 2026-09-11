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
 * 裁决落地组（烧录验证轮，A0 遗留①②，同款策略模型复演）：
 *   裁决1 BATTERY_FAULT 即停高功耗（§7.1 字面）：故障当拍 WSS 即停恰一次
 *   （无线已启动门控）、宽限后仍走既有受控休眠、恢复不自动重启无线（v1
 *   保守策略，重启靠 PWR 重新上电——与 KEY 深睡唤醒同款）。
 *   裁决2 ALLOW_RADIO_START 运行期接线：启动采样失败后恢复的两条路径
 *   （首批恢复启动判定 / recovery 稳定计时）ALLOW → start_radio 真实执行
 *   恰一次；附 10s 常规节奏饿死 recovery 计时的语义钉（fast 条件依据）。
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

/* 裁决1/2（烧录验证轮落地）桩：WSS 即停调用计数（= power_handle_actions 的
 * cdt_wss_stop，s_radio_started 门控内）；运行期 start_radio 计数；无线启动
 * 镜像（main 的 s_radio_started：boot 放行 / 运行期 ALLOW 置位）。 */
static int g_wss_stop_calls;
static int g_radio_start_calls;
static int g_radio_started;

static void model_reset(void)
{
    g_sleep_exec_calls = 0;
    g_sleep_latched = 0;
    memset(&g_last_sample, 0, sizeof g_last_sample);
    g_runtime_battery_valid = -1;
    g_wss_stop_calls = 0;
    g_radio_start_calls = 0;
    g_radio_started = 0;
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

/* main.c power_handle_actions（R3a 掩码 → 锁存；裁决1 故障即停；裁决2 运行期
 * 开无线）+ 调用点执行。与 firmware/main/main.c 同拍语义镜像。 */
static void model_dispatch(cdt_power_action_t acts)
{
    if (app_power_sleep_requested(acts)) {
        g_sleep_latched = 1; /* s_sleep_latched = true（同拍先置位） */
    }
    /* 裁决1：BATTERY_FAULT 当拍立即停 WSS（s_radio_started 门控，未启动不空停）*/
    if (app_power_fault_stop_radio_now(acts) && g_radio_started) {
        g_wss_stop_calls++; /* cdt_wss_stop()；最终 wss+net 关闭仍归 §7.3 顺序 */
    }
    /* 裁决2：无线未启动 + FSM 发 ALLOW + 同拍无睡眠请求 → start_radio 恰一次 */
    if (!g_sleep_latched && app_power_runtime_radio_allow(acts, g_radio_started)) {
        g_radio_start_calls++;
        g_radio_started = 1;
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
    if (*radio_allowed) {
        g_radio_started = 1; /* app_task：radio_ok → start_radio() */
    }
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
    (void)cdt_power_step(f, &in, 0); /* 健康启动 → ACTIVE（发 ALLOW） */
    g_radio_started = 1;             /* 无线已启动（healthy boot → start_radio） */
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
 * 随后有效样本恢复 → FSM 发 ALLOW_RADIO_START（运行期由裁决2 接线真正
 * start_radio，另见 adj2 测试组；本测试钉 FSM 侧语义）。 */
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

/* ---------- 裁决1：BATTERY_FAULT 即停高功耗（§7.1 字面，A0 遗留①） ---------- */

/* 故障成立当拍立即停 WSS（不等宽限后的 §7.3 停止顺序）；宽限走完仍走既有
 * 受控休眠路径；故障谓词不重复停（最终 wss+net 关闭归 §7.3 停止顺序）。 */
static void test_adj1_fault_stops_wss_immediately_then_sleeps(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t fail = { -1, 0, 0 };
    cdt_power_action_t a = CDT_POWER_ACT_NONE;
    int stops_at_fault = -1;
    int64_t t;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    fsm_healthy_boot(&fsm); /* 4000mV → ACTIVE，无线已启动 */
    check(g_radio_started == 1,
          "adj1/immediate: 健康启动后无线已启动（前置）", "radio 未启动");

    for (t = 1000; t <= 60000; t += 1000) {
        a = model_loop_sample(&fsm, t, &fail);
        if (HAS(a, CDT_POWER_ACT_BATTERY_FAULT)) {
            stops_at_fault = g_wss_stop_calls; /* 故障当拍快照 */
            break;
        }
    }
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT) && stops_at_fault == 1,
          "adj1/immediate: 故障当拍 WSS 即停恰一次（§7.1 关高功耗活动）",
          "故障当拍未即停");

    for (; t <= 60000; t += 1000) {
        a = model_loop_sample(&fsm, t, &fail);
        if (g_sleep_exec_calls > 0) {
            break; /* 真机 do_sleep_sequence 不返回 */
        }
    }
    check(HAS(a, CDT_POWER_ACT_CONTROLLED_SLEEP) && g_sleep_exec_calls == 1,
          "adj1/immediate: 宽限后受控休眠真实执行（既有路径不变）", "未受控休眠");
    check(g_wss_stop_calls == 1,
          "adj1/immediate: 故障谓词只停一次（最终关闭归 §7.3 停止顺序）", "重复停");
}

/* 无线从未启动（启动首批即故障）：不空停（固件门控 s_radio_started；
 * cdt_wss_stop 未运行只会返回 INVALID_STATE，模型按门控不调用），
 * 受控休眠仍执行。 */
static void test_adj1_fault_without_radio_no_stop_but_still_sleeps(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t fail = { -1, 0, 0 };
    cdt_power_action_t a;
    int radio = -1;
    int64_t t;
    int saw_fault = 0;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    (void)model_boot_check(&fsm, 1000, &fail, &radio); /* 首批失败，无线未启动 */
    check(g_radio_started == 0, "adj1/no_radio: 无线从未启动（前置）", "radio 已启动");

    for (t = 2000; t <= 60000; t += 1000) {
        a = model_loop_sample(&fsm, t, &fail);
        if (HAS(a, CDT_POWER_ACT_BATTERY_FAULT)) {
            saw_fault = 1;
        }
        if (g_sleep_exec_calls > 0) {
            break;
        }
    }
    check(saw_fault == 1 && g_wss_stop_calls == 0,
          "adj1/no_radio: 无线未启动不空停 WSS", "空停或未触发故障");
    check(g_sleep_exec_calls == 1,
          "adj1/no_radio: 受控休眠仍真实执行", "未休眠");
    check(g_radio_start_calls == 0,
          "adj1/no_radio: 故障路径不触发运行期开无线", "误开无线");
}

/* 宽限内有效样本恢复：BATTERY_FAULT_RECOVERED；v1 不自动重启无线——FSM 恢复
 * 不发 ALLOW_RADIO_START（重启保底 = PWR 重新上电，与 KEY 深睡唤醒同款保守
 * 策略），谓词层已启动也不重启；不睡眠。 */
static void test_adj1_recover_does_not_restart_wss(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t fail = { -1, 0, 0 };
    cdt_power_action_t a = CDT_POWER_ACT_NONE;
    int64_t t;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    fsm_healthy_boot(&fsm);

    for (t = 1000; t <= 60000; t += 1000) {
        a = model_loop_sample(&fsm, t, &fail);
        if (HAS(a, CDT_POWER_ACT_BATTERY_FAULT)) {
            break;
        }
    }
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT) && g_wss_stop_calls == 1,
          "adj1/recover: 故障成立且 WSS 已即停（前置）", "前置不成立");

    /* 宽限 10s 内（故障当拍 +3s）恢复样本到达 */
    a = model_loop_sample(&fsm, t + 3000, &(battery_stub_t){ 0, 4100, 1 });
    check(HAS(a, CDT_POWER_ACT_BATTERY_FAULT_RECOVERED),
          "adj1/recover: 宽限内有效样本 → FAULT_RECOVERED", "未恢复");
    check(!HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START),
          "adj1/recover: FSM 恢复不发 ALLOW_RADIO_START（不自动重启的 FSM 侧依据）",
          "恢复发了 ALLOW");
    check(g_radio_start_calls == 0,
          "adj1/recover: 无运行期重启（v1 保守：重启靠 PWR 重新上电）", "误重启");
    check(g_wss_stop_calls == 1,
          "adj1/recover: 已停的 WSS 不回滚", "状态回滚");
    check(g_sleep_exec_calls == 0,
          "adj1/recover: 恢复路径不睡眠", "误睡眠");
}

/* ---------- 裁决2：ALLOW_RADIO_START 运行期接线（A0 遗留②） ---------- */

/* 场景「启动采样失败后恢复」：首批失败（无线未启动）→ 主循环首批有效样本
 * 完成启动判定发 ALLOW → start_radio 真被调用恰一次（旧代码仅日志）。 */
static void test_adj2_boot_fail_recover_starts_radio(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t fail = { -1, 0, 0 };
    battery_stub_t ok = { 0, 4100, 1 };
    cdt_power_action_t a;
    int radio = -1;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, false);
    (void)model_boot_check(&fsm, 1000, &fail, &radio);
    check(g_radio_started == 0,
          "adj2/boot_fail: 首批失败无线未启动（前置）", "radio 已启动");

    /* 主循环 1Hz 故障节奏（invalid 末样本 mv=0 → should_sample_fast(0)=true）
     * 下一批恢复：FSM 完成启动判定发 ALLOW */
    a = model_loop_sample(&fsm, 2000, &ok);
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START) && g_radio_start_calls == 1,
          "adj2/boot_fail: 运行期 ALLOW → start_radio 真实执行恰一次",
          "ALLOW 未接线");
    check(g_radio_started == 1 && g_sleep_exec_calls == 0,
          "adj2/boot_fail: 无线置为已启动且不睡眠", "状态/睡眠错误");

    /* 谓词单源：已启动不得因 ALLOW 重复启动 */
    check(!app_power_runtime_radio_allow(CDT_POWER_ACT_ALLOW_RADIO_START, true),
          "adj2/boot_fail: 已启动时谓词拒绝重复启动", "谓词过宽");
}

/* recovery 稳定计时子路径（wake_low_hint + 首批失败）：1Hz 节奏（main.c fast
 * 条件含 boot_recovery_required）下 ≥recovery_mv 稳定 10s → ALLOW → 运行期
 * start_radio 恰一次、不开睡眠。 */
static void test_adj2_recovery_timer_starts_radio_with_fast_cadence(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t fail = { -1, 0, 0 };
    battery_stub_t ok = { 0, 4100, 1 };
    cdt_power_action_t a = CDT_POWER_ACT_NONE;
    int64_t t;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, true); /* RTC 低压唤醒 hint */
    (void)model_boot_check(&fsm, 1000, &fail, &(int){ -1 });
    check(g_radio_started == 0,
          "adj2/recov_timer: 首批失败无线未启动（前置）", "radio 已启动");

    for (t = 2000; t <= 30000; t += 1000) { /* 1Hz = main fast 节奏 */
        a = model_loop_sample(&fsm, t, &ok);
        if (HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START)) {
            break;
        }
    }
    check(HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START),
          "adj2/recov_timer: 1Hz 下 recovery 稳定 10s → ALLOW", "ALLOW 未发出");
    check(g_radio_start_calls == 1 && g_radio_started == 1,
          "adj2/recov_timer: 运行期 start_radio 恰一次", "未启动或重复启动");
    check(g_sleep_exec_calls == 0,
          "adj2/recov_timer: recovery 通过不睡眠", "误睡眠");
    check(t <= 13000,
          "adj2/recov_timer: 首个有效样本后 ~11s 内放行（recov 2000 起稳定 10s）",
          "放行过晚");
}

/* 语义钉（main.c fast 条件的依据）：recovery 计时在跑时 10s 常规节奏每次采样
 * 构成缺测（gap>2s）→ recov_running 被重置 → ALLOW 永不发出。这就是
 * boot_recovery_required 必须进 fast 条件的原因（cdt_power.c 冻结语义，
 * 本测试只钉现象防回归认知，不改共享 FSM）。 */
static void test_adj2_normal_cadence_starves_recovery_documentation_pin(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_params_t p;
    battery_stub_t ok = { 0, 4100, 1 };
    cdt_power_action_t a;
    int64_t t;
    int saw_allow = 0;

    model_reset();
    cdt_power_params_init(&p);
    cdt_power_init(&fsm, &p, true);
    (void)model_boot_check(&fsm, 1000, &(battery_stub_t){ -1, 0, 0 }, &(int){ -1 });

    for (t = 2000; t <= 62000; t += 10000) { /* 10s 常规节奏 */
        a = model_loop_sample(&fsm, t, &ok);
        if (HAS(a, CDT_POWER_ACT_ALLOW_RADIO_START)) {
            saw_allow = 1;
        }
    }
    check(!saw_allow && fsm.state == CDT_POWER_BOOT_CHECK,
          "adj2/pin: 10s 节奏下 recovery 被缺测重置、ALLOW 永不发出"
          "（boot_recovery_required 必须按 1Hz 快采）",
          "语义与预期不符");
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
    test_adj1_fault_stops_wss_immediately_then_sleeps();
    test_adj1_fault_without_radio_no_stop_but_still_sleeps();
    test_adj1_recover_does_not_restart_wss();
    test_adj2_boot_fail_recover_starts_radio();
    test_adj2_recovery_timer_starts_radio_with_fast_cadence();
    test_adj2_normal_cadence_starves_recovery_documentation_pin();

    printf("\n%d checks: %d passed, %d failed\n", passes + failures, passes,
           failures);
    return failures == 0 ? 0 : 1;
}
