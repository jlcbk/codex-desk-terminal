/*
 * test_power_exec_pure.c — P5.3 电源执行器主机端测试（A3）
 *
 * 用法：test_power_exec_pure（真实退出码；任何 FAIL → exit 1）。
 * 真源：docs/DEVELOPMENT_PLAN.md §7.3 停止顺序全表、§7.2
 * final_frame_timeout_ms、docs/HARDWARE.md §1.2/§1.5/§4、
 * shared/power/cdt_power.h（P5.1 冻结 FSM 动作位掩码）。
 *
 * 两个编译变体（scripts/build_power_tests.sh）：
 *   A. 默认（CDT_CFG_KEY_DEEP_WAKE 未定义 → 头文件默认 0）：
 *      链接桩计数断言 esp_sleep 唤醒配置调用 == 0——即使运行期标志置 true
 *      也无效（编译期门禁）；KEY 深睡唤醒关闭期间保底恢复 = PWR 重新上电。
 *   B. -DCDT_CFG_KEY_DEEP_WAKE=1（仅测试变体；生产启用条件 = P5.4 真机实测
 *      通过，HARDWARE §4.2）：断言 ext0(GPIO18, 低) 恰在运行期标志为真时
 *      配置一次（双重门禁的运行期半边）。
 *
 * 覆盖：§7.3 顺序（CRITICAL 驱动 FSM→执行器全 8 步轨迹）、末帧 flush
 * 成功/超时/无句柄三路（超时仍进 sleep）、1ms 粒度超时判定、低压原因
 * 标记编码/写入/读回/清除/损坏、BOOT_CHECK 唤醒门禁（标记存在→拒无线）、
 * 顺序违规守卫（缺 prepare/末帧拒绝深睡）、PA=46 安全电平计数。
 */
#include <stdio.h>
#include <string.h>

#include "cdt_power_exec.h"
#include "esp_stub.h"

static int g_pass;
static int g_fail;

static void check(int cond, const char *name, const char *detail)
{
    if (cond) {
        g_pass++;
        printf("[PASS] %s\n", name);
    } else {
        g_fail++;
        printf("[FAIL] %s — %s\n", name, detail ? detail : "");
    }
}

/* ---------------- 公共注入（假时钟 / 内存标记 / 计数回调）----------- */

static int64_t g_now;
static int g_wait_calls;
static int g_poll_calls;

static bool poll_true(void *u) { (void)u; return true; }
static bool poll_never(void *u) { (void)u; return false; }
static bool poll_third(void *u)
{
    (void)u;
    g_poll_calls++;
    return g_poll_calls >= 3;
}
static void wait_ms_count(void *u, uint32_t ms)
{
    (void)u;
    g_wait_calls++;
    g_now += (int64_t)ms; /* 1ms 粒度假时钟 */
}
static int64_t now_ms_fn(void *u) { (void)u; return g_now; }

static uint32_t s_mem;
static bool mem_read(void *u, uint32_t *o) { (void)u; *o = s_mem; return true; }
static bool mem_write(void *u, uint32_t r) { (void)u; s_mem = r; return true; }
static void mem_clear(void *u) { (void)u; s_mem = 0u; }
static bool fail_read(void *u, uint32_t *o) { (void)u; (void)o; return false; }

static const cdt_pexec_reason_io_t MEM_IO = { NULL, mem_read, mem_write, mem_clear };
static const cdt_pexec_reason_io_t FAIL_IO = { NULL, fail_read, mem_write, mem_clear };

static int g_cancel_calls;
static void on_cancel_reconnect(void *u) { (void)u; g_cancel_calls++; }
static const cdt_power_exec_prepare_ops_t PREP_OPS = { NULL, on_cancel_reconnect };

static int g_stop_calls;
static int g_periph_calls;
static int g_lcd_calls;
static cdt_power_exec_lcd_mode_t g_lcd_last;
static void on_stop_radio(void *u) { (void)u; g_stop_calls++; }
static void on_power_off(void *u) { (void)u; g_periph_calls++; }
static void on_lcd_final(void *u, cdt_power_exec_lcd_mode_t m)
{
    (void)u;
    g_lcd_calls++;
    g_lcd_last = m;
}
static const cdt_power_exec_sleep_ops_t SLEEP_OPS = {
    NULL, on_stop_radio, on_power_off, on_lcd_final
};

/* 读默认 RTC 存储的门禁包装（定义在执行器 suite 区；此处先声明）*/
static bool s_rtc_gate_denies(bool *hint);

/* KEY 运行期标志：宏关闭变体置 true（证编译期门禁）；宏开启变体置 false
 * （true 情形由专用 suite 覆盖，保持公共 suite 的桩计数干净）。 */
#if CDT_CFG_KEY_DEEP_WAKE
#define CFG_KEY_FLAG false
#else
#define CFG_KEY_FLAG true
#endif

static const cdt_pexec_step_t EXPECTED_ORDER[8] = {
    CDT_PEXEC_STEP_BLOCK_NEW_UPDATES,
    CDT_PEXEC_STEP_FINAL_FRAME,
    CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO,
    CDT_PEXEC_STEP_POWER_OFF_PERIPH,
    CDT_PEXEC_STEP_LCD_FINAL,
    CDT_PEXEC_STEP_GPIO_SAFE_LEVELS,
    CDT_PEXEC_STEP_CONFIG_WAKE,
    CDT_PEXEC_STEP_ENTER_DEEP_SLEEP
};

static void check_trace_is_full_order(const char *name)
{
    const cdt_pexec_trace_t *tr = cdt_power_exec_trace();
    int i;
    bool ok = (tr->len == 8) && !tr->overflow;
    for (i = 0; ok && i < 8; i++) {
        ok = tr->steps[i] == EXPECTED_ORDER[i];
    }
    check(ok, name, "轨迹应恰为 §7.3 全表 8 步且顺序一致");
}

/* ---------------- 纯逻辑：顺序编排 ---------------- */

static void test_seq_order(void)
{
    cdt_pexec_seq_t s;
    int i;
    bool ok = true;
    cdt_pexec_seq_init(&s, 1000u);
    for (i = 0; i < 8; i++) {
        if (cdt_pexec_seq_current(&s) != EXPECTED_ORDER[i]) {
            ok = false;
            break;
        }
        if (EXPECTED_ORDER[i] == CDT_PEXEC_STEP_FINAL_FRAME) {
            cdt_pexec_seq_final_frame_settled(&s, true, false, false);
        } else {
            (void)cdt_pexec_seq_step_done(&s);
        }
    }
    check(ok, "seq_order_matches_7_3_table", "应为 §7.3 全表 8 步顺序");
    check(cdt_pexec_seq_terminal(&s) &&
              cdt_pexec_seq_current(&s) == CDT_PEXEC_STEP_NONE,
          "seq_terminal_after_deep_sleep", "第⑧步后应为终态");
    check(!cdt_pexec_seq_step_done(&s), "seq_terminal_step_done_rejected",
          "终态后 step_done 应拒绝");
}

static void test_seq_final_frame_gate(void)
{
    cdt_pexec_seq_t s;
    cdt_pexec_seq_init(&s, 1000u);
    check(cdt_pexec_seq_step_done(&s), "seq_block_step_advances", "步骤①应推进");
    check(cdt_pexec_seq_current(&s) == CDT_PEXEC_STEP_FINAL_FRAME,
          "seq_next_is_final_frame", "步骤②应为末帧");
    check(!cdt_pexec_seq_step_done(&s), "seq_final_frame_not_skippable",
          "末帧步不允许普通 step_done 跳过");
    check(cdt_pexec_seq_current(&s) == CDT_PEXEC_STEP_FINAL_FRAME,
          "seq_final_frame_still_current", "跳过失败后仍停在末帧步");
    cdt_pexec_seq_final_frame_settled(&s, false, true, false);
    check(cdt_pexec_seq_current(&s) == CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO &&
              s.ff_timed_out && !s.ff_flushed,
          "seq_settle_timeout_advances", "超时收敛后应推进到步骤③");
}

/* ---------------- 纯逻辑：1ms 粒度超时判定 ---------------- */

static void test_timeout_due(void)
{
    check(!cdt_pexec_timeout_due(100, 100 + 999, 1000),
          "timeout_not_due_1ms_before", "999ms 不应到期");
    check(cdt_pexec_timeout_due(100, 100 + 1000, 1000),
          "timeout_due_exact_1ms_granularity", "恰好 1000ms 应到期（同毫秒语义）");
    check(!cdt_pexec_timeout_due(100, 50, 1000),
          "timeout_clock_regression_not_due", "时钟倒退防御：不触发");
    check(cdt_pexec_timeout_due(100, 100, 0),
          "timeout_zero_immediate", "timeout=0 立即到期");
}

/* ---------------- 纯逻辑：末帧等待三路 ---------------- */

static void test_wait_success_path(void)
{
    bool f = false, t = true, a = true;
    cdt_pexec_wait_ops_t ops;
    g_now = 1000;
    g_poll_calls = 0;
    g_wait_calls = 0;
    ops.user = NULL;
    ops.poll_flush = poll_third;
    ops.wait_ms = wait_ms_count;
    ops.now_ms = now_ms_fn;
    cdt_pexec_final_frame_wait(&ops, g_now, 1000u, &f, &t, &a);
    check(f && !t && !a, "wait_flushed", "第 3 次 poll 应判定 flush 完成");
    check(g_poll_calls == 3 && g_wait_calls == 2,
          "wait_success_counts", "应 poll 3 次、期间恰好 2 次 1ms 等待");
}

static void test_wait_timeout_path(void)
{
    bool f = true, t = false, a = true;
    cdt_pexec_wait_ops_t ops;
    g_now = 5000;
    g_wait_calls = 0;
    ops.user = NULL;
    ops.poll_flush = poll_never;
    ops.wait_ms = wait_ms_count;
    ops.now_ms = now_ms_fn;
    cdt_pexec_final_frame_wait(&ops, g_now, 1000u, &f, &t, &a);
    check(!f && t && !a, "wait_timeout", "1s 内未完成应走超时路径");
    check(g_wait_calls == 1000, "wait_timeout_1000x1ms",
          "应恰好 1000 次 1ms 等待（粒度可测）");
}

static void test_wait_zero_and_degenerate(void)
{
    bool f = true, t = false, a = true;
    cdt_pexec_wait_ops_t ops;
    g_now = 7000;
    g_wait_calls = 0;
    ops.user = NULL;
    ops.poll_flush = poll_never;
    ops.wait_ms = wait_ms_count;
    ops.now_ms = now_ms_fn;
    cdt_pexec_final_frame_wait(&ops, g_now, 0u, &f, &t, &a);
    check(t && !f && g_wait_calls == 0, "wait_zero_timeout_immediate",
          "timeout=0 立即超时、零等待");

    f = true; t = false; a = false;
    ops.poll_flush = NULL; /* 无句柄 */
    cdt_pexec_final_frame_wait(&ops, g_now, 1000u, &f, &t, &a);
    check(!f && !t && a, "wait_no_handle_abandoned", "无句柄应为放弃路径");

    f = true; t = false; a = false;
    ops.poll_flush = poll_never;
    ops.wait_ms = NULL; /* 无等待注入：只查一次到期 */
    ops.now_ms = now_ms_fn;
    cdt_pexec_final_frame_wait(&ops, g_now, 1000u, &f, &t, &a);
    check(!f && !t && !a, "wait_no_waitop_single_check",
          "无等待注入时只做单次判定（不忙等、不误判超时）");
}

/* ---------------- 纯逻辑：低压原因标记与唤醒门禁 ---------------- */

static void test_reason_codec(void)
{
    bool low = false;
    check(cdt_pexec_reason_encode(true) == 0xC9u &&
              cdt_pexec_reason_encode(false) == 0xC1u,
          "reason_encode_values", "应为 0xC9（低压）/0xC1（非低压）");
    check(cdt_pexec_reason_decode(0xC9u, &low) && low,
          "reason_decode_low", "0xC9 应解码为有效+低压");
    check(cdt_pexec_reason_decode(0xC1u, &low) && !low,
          "reason_decode_not_low", "0xC1 应解码为有效+非低压");
    check(!cdt_pexec_reason_decode(0x00u, &low), "reason_reject_zero",
          "全零应无效");
    check(!cdt_pexec_reason_decode(0x10u, &low), "reason_reject_bad_magic",
          "魔数错应无效");
    check(!cdt_pexec_reason_decode(0xC2u, &low), "reason_reject_bad_version",
          "版本错应无效");
    check(!cdt_pexec_reason_decode(0x81u, &low), "reason_reject_magic_8",
          "0x8 魔数应无效");
}

static void test_reason_io_memory(void)
{
    bool low = false;
    s_mem = 0u;
    check(cdt_pexec_reason_write(&MEM_IO, true) && s_mem == 0xC9u,
          "reason_write_encodes", "写入应落编码字");
    check(cdt_pexec_reason_read(&MEM_IO, &low) && low,
          "reason_read_roundtrip", "写读回环：有效+低压");
    check(cdt_pexec_reason_write(&MEM_IO, false) &&
              cdt_pexec_reason_read(&MEM_IO, &low) && !low,
          "reason_overwrite", "覆写为非低压后读回");
    cdt_pexec_reason_clear(&MEM_IO);
    check(s_mem == 0u && !cdt_pexec_reason_read(&MEM_IO, &low),
          "reason_clear_invalidates", "清除后应无有效标记");
    s_mem = 0x100u | 0xC9u;
    check(!cdt_pexec_reason_read(&MEM_IO, &low),
          "reason_dirty_high_bits_rejected", "脏高位按无效标记处理");
    check(!cdt_pexec_reason_read(&FAIL_IO, &low), "reason_read_fail_reported",
          "读取失败应显式返回失败");
}

static void test_wake_gate(void)
{
    bool hint = true;
    s_mem = (uint32_t)cdt_pexec_reason_encode(true);
    check(!cdt_pexec_wake_gate_allow_radio(&MEM_IO, &hint) && hint,
          "gate_low_mark_denies_radio", "低压标记存在→拒无线且给 hint");
    s_mem = 0u;
    hint = true;
    check(cdt_pexec_wake_gate_allow_radio(&MEM_IO, &hint) && !hint,
          "gate_no_mark_allows", "无标记→允许（交 FSM 首样本判定）");
    s_mem = (uint32_t)cdt_pexec_reason_encode(false);
    hint = true;
    check(cdt_pexec_wake_gate_allow_radio(&MEM_IO, &hint) && !hint,
          "gate_nonlow_mark_allows", "非低压标记→允许");
    s_mem = 0xFFFFu;
    check(cdt_pexec_wake_gate_allow_radio(&MEM_IO, &hint),
          "gate_corrupt_mark_allows", "损坏标记→允许（无效读数路径）");
    check(cdt_pexec_wake_gate_allow_radio(&FAIL_IO, &hint),
          "gate_read_fail_allows", "读取失败→允许");
    check(cdt_pexec_wake_gate_allow_radio(NULL, &hint),
          "gate_null_io_allows", "无存储注入→允许（防御）");
}

/* ---------------- 执行器（链接桩）：FSM 驱动 + §7.3 顺序 ---------------- */

/* SLEEP_PREP 进入时刻（drive_to_sleep_prep 写出；超时断言用）*/
static int64_t g_prep_since;

/* 驱动 P5.1 FSM 走"运行中持续低压"路径（非低压唤醒冷启动）：
 * t=0 健康 3900mV 越过 BOOT_CHECK（先 ADC 后无线）；t=1000..31000 连续
 * 3500mV → crit_start=1000，t=31000 恰好 30s 到期 → CRITICAL 锁存；
 * 下一拍 t=32000 → SLEEP_PREP。 */
static cdt_power_action_t drive_to_sleep_prep(cdt_power_fsm_t *fsm)
{
    cdt_power_input_t in;
    cdt_power_action_t acc = CDT_POWER_ACT_NONE;
    int64_t t;
    memset(&in, 0, sizeof(in));
    in.kind = CDT_POWER_IN_SAMPLE;
    in.sample.valid = true;
    in.sample.battery_mv = 3900u; /* >3600：健康启动，进 ACTIVE */
    in.sample.at_ms = 0;
    acc |= cdt_power_step(fsm, &in, 0);
    in.sample.battery_mv = 3500u; /* ≤3600：计持续低压（§7.2）*/
    for (t = 1000; t <= 31000; t += 1000) {
        in.sample.at_ms = t;
        acc |= cdt_power_step(fsm, &in, t);
    }
    check((acc & CDT_POWER_ACT_ENTER_CRITICAL) != 0,
          "fsm_critical_at_30s_exact", "t=31000 恰好 30s 到期应进 CRITICAL");
    in.sample.at_ms = 32000;
    acc |= cdt_power_step(fsm, &in, 32000);
    check((acc & CDT_POWER_ACT_BEGIN_SLEEP_PREP) != 0 &&
              fsm->state == CDT_POWER_SLEEP_PREP,
          "fsm_sleep_prep_next_step", "CRITICAL 观察步后应进 SLEEP_PREP");
    g_prep_since = 32000;
    return acc;
}

static cdt_power_action_t exec_begin(cdt_power_fsm_t *fsm)
{
    cdt_power_params_t p;
    cdt_power_action_t acc;
    cdt_power_params_init(&p);
    cdt_power_init(fsm, &p, false);
    acc = drive_to_sleep_prep(fsm);
    cdt_power_exec_flow_reset(CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);
    cdt_power_exec_prepare(acc, &PREP_OPS);
    return acc;
}

/* 完整 §7.3 全表顺序（末帧 flush 成功路径，FSM 终态对齐 DEEP_SLEEP）*/
static void test_executor_full_order_flush_ok(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;
    cdt_power_exec_stats_t st;
    cdt_power_input_t ev;
    cdt_power_action_t a2;
    bool flushed;
    int ds0;
    bool hint = false;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = poll_true;
    wops.wait_ms = wait_ms_count;
    wops.now_ms = now_ms_fn;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD;
    cfg.key_deep_wake = CFG_KEY_FLAG;
    cfg.low_battery_reason = true;

    g_now = 100000;
    g_cancel_calls = 0;
    g_stop_calls = 0;
    g_periph_calls = 0;
    g_lcd_calls = 0;
    cdt_esp_stub_reset();

    (void)exec_begin(&fsm);
    cdt_power_exec_stats_get(&st);
    check(st.updates_frozen && cdt_power_exec_updates_frozen() &&
              st.reconnect_cancelled && g_cancel_calls == 1,
          "prepare_freezes_and_cancels", "步骤①应冻结新更新并取消重连");

    flushed = cdt_power_exec_final_frame(&wops, 0u /*0 → 默认 1000ms*/);
    check(flushed, "final_frame_flush_ok", "flush 完成应返回 true");

    /* FSM 对齐：末帧完成事件 → DEEP_SLEEP（P5.1 动作位）*/
    memset(&ev, 0, sizeof(ev));
    ev.kind = CDT_POWER_IN_EVENT;
    ev.event = CDT_POWER_EVT_FINAL_FRAME_DONE;
    a2 = cdt_power_step(&fsm, &ev, g_now);
    check((a2 & CDT_POWER_ACT_DEEP_SLEEP_READY) != 0 &&
              fsm.state == CDT_POWER_DEEP_SLEEP,
          "fsm_deep_sleep_ready_after_flush", "FSM 应给出 DEEP_SLEEP_READY");

    ds0 = g_esp_stub.deep_sleep_start_calls;
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, cdt_power_exec_rtc_reason_io());
    check(g_esp_stub.deep_sleep_start_calls == ds0 + 1,
          "deep_sleep_entered_once", "步骤⑧应恰好一次 esp_deep_sleep_start");
    check(g_esp_stub.pa46_low_calls == 1 &&
              g_esp_stub.gpio_set_direction_calls == 1,
          "gpio_safe_pa46_low", "步骤⑥应将 PA(GPIO46) 输出低一次");
    check(g_stop_calls == 1 && g_periph_calls == 1 &&
              g_lcd_calls == 1 && g_lcd_last == CDT_POWER_EXEC_LCD_HOLD,
          "stop_periph_lcd_called_in_order", "步骤③④⑤注入各恰好一次");
    check_trace_is_full_order("executor_order_full_7_3_flush_ok");

    /* RTC 原因标记已写 → BOOT_CHECK 门禁拒绝开无线（低压反弹不反复启动）*/
    check(s_rtc_gate_denies(&hint), "boot_gate_denies_after_mark", 
          "写标记后门禁应拒绝无线且 hint=true");
    cdt_pexec_reason_clear(cdt_power_exec_rtc_reason_io());
    check(cdt_power_exec_boot_gate_allow_radio(cdt_power_exec_rtc_reason_io(),
                                               &hint) && !hint,
          "boot_gate_allows_after_clear", "清除标记后门禁放行");
}

/* 门禁读默认 RTC 存储的小包装（保持上一测试函数可读）*/
static bool s_rtc_gate_denies(bool *hint)
{
    return !cdt_power_exec_boot_gate_allow_radio(cdt_power_exec_rtc_reason_io(),
                                                 hint);
}

/* 末帧超时路径：§7.2 超时 1s 失败也睡；FSM 超时动作位同现 */
static void test_executor_final_frame_timeout_still_sleeps(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;
    cdt_power_exec_stats_t st;
    cdt_power_action_t a3;
    bool flushed;
    int ds0;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = poll_never;
    wops.wait_ms = wait_ms_count;
    wops.now_ms = now_ms_fn;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_OFF;
    cfg.key_deep_wake = CFG_KEY_FLAG;
    cfg.low_battery_reason = true;

    g_now = 200000;
    g_wait_calls = 0;
    cdt_esp_stub_reset();

    (void)exec_begin(&fsm);
    flushed = cdt_power_exec_final_frame(&wops, 1000u);
    check(!flushed, "timeout_returns_not_flushed", "超时应返回未完成");
    check(g_wait_calls == 1000, "timeout_wait_count_1ms_granularity",
          "超时前应恰好 1000 次 1ms 注入等待");
    cdt_power_exec_stats_get(&st);
    check(st.final_frame_timed_out && !st.final_frame_flushed,
          "timeout_stats_recorded", "统计应记录超时路径");

    /* FSM 对齐：SLEEP_PREP 于 g_prep_since 进入，超时恰好 1s 后到期 */
    a3 = cdt_power_step(&fsm, NULL, g_prep_since + 1000);
    check((a3 & CDT_POWER_ACT_FINAL_FRAME_TIMEOUT) != 0 &&
              (a3 & CDT_POWER_ACT_DEEP_SLEEP_READY) != 0,
          "fsm_timeout_yields_deep_sleep_ready", "FSM：超时与可入睡同现");

    ds0 = g_esp_stub.deep_sleep_start_calls;
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, &MEM_IO);
    check(g_esp_stub.deep_sleep_start_calls == ds0 + 1,
          "timeout_still_enters_deep_sleep", "§7.2：末帧超时失败也睡");
    check_trace_is_full_order("executor_order_full_7_3_timeout");
}

/* 无 flush 句柄：放弃路径（非超时），仍必须继续入睡 */
static void test_executor_final_frame_no_handle(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;
    cdt_power_exec_stats_t st;
    int ds0;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = NULL; /* 无句柄 */
    wops.wait_ms = wait_ms_count;
    wops.now_ms = now_ms_fn;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD;
    cfg.low_battery_reason = false;

    g_now = 300000;
    cdt_esp_stub_reset();

    (void)exec_begin(&fsm);
    (void)cdt_power_exec_final_frame(&wops, 1000u);
    cdt_power_exec_stats_get(&st);
    check(st.final_frame_abandoned && !st.final_frame_timed_out,
          "no_handle_abandoned_not_timeout", "无句柄应记放弃而非超时");

    ds0 = g_esp_stub.deep_sleep_start_calls;
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, &MEM_IO);
    check(g_esp_stub.deep_sleep_start_calls == ds0 + 1,
          "abandoned_still_sleeps", "放弃末帧后仍进深睡（§7.3 完成/放弃）");
    check_trace_is_full_order("executor_order_full_7_3_abandoned");
}

/* 顺序违规守卫：缺 prepare/末帧时拒绝深睡（绝不静默跳段入睡）*/
static void test_executor_order_violation_guards(void)
{
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;
    cdt_power_exec_stats_t st;
    int ds0;
    bool flushed;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = poll_true;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD;
    cfg.key_deep_wake = CFG_KEY_FLAG;

    cdt_esp_stub_reset();
    ds0 = g_esp_stub.deep_sleep_start_calls;

    cdt_power_exec_flow_reset(1000u);
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, &MEM_IO);
    cdt_power_exec_stats_get(&st);
    check(g_esp_stub.deep_sleep_start_calls == ds0 && st.order_violation,
          "violation_sleep_without_prepare_rejected",
          "缺步骤①②直接 sleep 应拒绝并置 order_violation");

    cdt_power_exec_flow_reset(1000u);
    flushed = cdt_power_exec_final_frame(&wops, 1000u);
    cdt_power_exec_stats_get(&st);
    check(!flushed && st.order_violation,
          "violation_final_frame_before_prepare_rejected",
          "跳过 prepare 直接末帧应拒绝");
    check(g_esp_stub.deep_sleep_start_calls == ds0,
          "violation_never_slept", "违规路径全程不得触发深睡");
}

/* 默认 RTC 存储（主机桩下为普通静态）：读/写/清除回环 */
static void test_executor_rtc_io_roundtrip(void)
{
    const cdt_pexec_reason_io_t *io = cdt_power_exec_rtc_reason_io();
    bool low = false;
    check(cdt_pexec_reason_write(io, true) &&
              cdt_pexec_reason_read(io, &low) && low,
          "rtc_io_roundtrip_write_read", "默认 RTC 注入应可写读回环");
    cdt_pexec_reason_clear(io);
    check(!cdt_pexec_reason_read(io, &low), "rtc_io_clear", "默认 RTC 注入可清除");
}

#if CDT_CFG_KEY_DEEP_WAKE
/* 变体 B：宏开启（仅测试变体）——运行期标志为第二重门禁 */
static void test_keywake_flag_true_configures_ext0(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = poll_true;
    wops.now_ms = now_ms_fn;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD;
    cfg.key_deep_wake = true;
    cfg.low_battery_reason = true;

    g_now = 400000;
    cdt_esp_stub_reset();
    (void)exec_begin(&fsm);
    (void)cdt_power_exec_final_frame(&wops, 1000u);
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, &MEM_IO);
    check(g_esp_stub.ext0_calls == 1 && g_esp_stub.ext0_last_gpio == 18 &&
              g_esp_stub.ext0_last_level == 0,
          "keywake_flag_true_ext0_gpio18_low",
          "宏开启+标志真：应恰配一次 ext0(GPIO18, 低电平)");
}

static void test_keywake_flag_false_zero_ext0(void)
{
    cdt_power_fsm_t fsm;
    cdt_power_exec_wait_ops_t wops;
    cdt_power_exec_sleep_config_t cfg;

    memset(&wops, 0, sizeof(wops));
    wops.poll_flush = poll_true;
    wops.now_ms = now_ms_fn;
    memset(&cfg, 0, sizeof(cfg));
    cfg.lcd_mode = CDT_POWER_EXEC_LCD_HOLD;
    cfg.key_deep_wake = false; /* 运行期门禁关闭 */
    cfg.low_battery_reason = true;

    g_now = 500000;
    cdt_esp_stub_reset();
    (void)exec_begin(&fsm);
    (void)cdt_power_exec_final_frame(&wops, 1000u);
    cdt_power_exec_sleep(&cfg, &SLEEP_OPS, &MEM_IO);
    check(g_esp_stub.ext0_calls == 0 &&
              g_esp_stub.deep_sleep_start_calls == 1,
          "keywake_flag_false_zero_ext0",
          "宏开启+标志假：零唤醒配置、仍正常入睡");
}
#endif

/* ---------------- 主入口 ---------------- */

int main(void)
{
    /* 纯逻辑（两变体都跑：宏只影响执行器编译分支）*/
    test_seq_order();
    test_seq_final_frame_gate();
    test_timeout_due();
    test_wait_success_path();
    test_wait_timeout_path();
    test_wait_zero_and_degenerate();
    test_reason_codec();
    test_reason_io_memory();
    test_wake_gate();

    /* 执行器（链接桩）*/
    test_executor_full_order_flush_ok();
    test_executor_final_frame_timeout_still_sleeps();
    test_executor_final_frame_no_handle();
    test_executor_order_violation_guards();
    test_executor_rtc_io_roundtrip();

#if CDT_CFG_KEY_DEEP_WAKE
    test_keywake_flag_true_configures_ext0();
    test_keywake_flag_false_zero_ext0();
#else
    /* 变体 A 收官断言：整轮所有流程（含 key_deep_wake=true 传入）后，
     * KEY 深睡唤醒配置调用仍为 0——编译期门禁生效（HARDWARE §4.2，
     * 开启条件 = P5.4 真机实测通过）。 */
    check(g_esp_stub.ext0_calls == 0,
          "key_macro_off_zero_esp_sleep_wake_calls",
          "宏关闭时全程零 esp_sleep 唤醒配置调用");
#endif

    printf("----\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
