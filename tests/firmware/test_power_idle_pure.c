/*
 * test_power_idle_pure.c — Wi-Fi 空闲动态降档纯决策主机端测试（ZC9 / P5.2 后半）
 *
 * 用法：test_power_idle_pure（无参数；真实退出码，任何 FAIL → exit 1）。
 * 决策单源：firmware/main/app_power_idle.{c,h}（main.c 与本测试同时编译，
 * 保证钉住的就是固件在用的判定，两处不漂移；同 app_power_policy 先例）。
 *
 * 覆盖（任务验收表逐项）：
 *   - 未到阈值 → MIN（无切换）；
 *   - 过阈值 → MAX（切换）；
 *   - 活动立即回 MIN（MAX 期间新快照/按键，绝对规则不受迟滞约束）；
 *   - 迟滞窗口内不反复（切 MAX 后 60s 内不允许再评估升档；窗口外恢复）；
 *   - 阈值边界（idle == threshold 恰好触发，≥ 语义）；
 *   - 无效时间戳防御（now<0 / threshold≤0 / 双活动无效 / 未来时间戳 /
 *     非法档位编码）。
 *
 * 输入约定见 app_power_idle.h：max_entered_ms >0=正处于 MAX（值为进入时刻），
 * <0=不在 MAX（绝对值=最近一次进入时刻），0=从未进入。
 */
#include <stdio.h>

#include "app_power_idle.h"

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

#define MS_MIN ((int64_t)60000)

static app_power_idle_verdict_t decide(int64_t now, int64_t rx, int64_t key,
                                       int64_t thr, int64_t entered)
{
    return app_power_idle_decide(now, rx, key, thr, entered);
}

/* ---------- 未到阈值 → MIN ---------- */

static void test_below_threshold_stays_min(void)
{
    /* 唯一活动源（快照）30s 前、阈值 1min：未达标 → MIN，无切换 */
    app_power_idle_verdict_t v = decide(30000, 0, 30000, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "below/key_only_min_no_switch", "30s < 60s 应 MIN 无切换");

    /* 双活动源取较新者；较新者 5s 前 → 未达标 */
    v = decide(30000, 25000, 18000, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "below/newest_source_wins", "应取较新 rx=25000（5s 前）→ MIN");

    /* 长期 MIN 稳态（从未进过 MAX，活动持续）：始终无切换 */
    v = decide(3600000, 3599000, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "below/steady_min_no_churn", "稳态 MIN 不应报需要切换");
}

/* ---------- 过阈值 → MAX ---------- */

static void test_over_threshold_switches_max(void)
{
    /* 快照 60s 前、阈值 1min → MAX 且需要切换 */
    app_power_idle_verdict_t v = decide(90000, 0, 30000, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "over/idle_switch_max", "idle 60s >= 60s 应升 MAX 需切换");

    /* 只有快照源（按键从未发生 = 0）：同样升 MAX */
    v = decide(120000, 55000, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "over/rx_only_source", "rx 唯一有效源，65s 前 ≥ 60s → MAX");

    /* 双源均达标：取较新者判定（rx 较旧、key 较新仍 ≥ 阈值） */
    v = decide(300000, 120000, 180000, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "over/both_sources_idle", "较新 key 120s 前 ≥ 60s → MAX");
}

/* ---------- 活动立即回 MIN（绝对规则，不受迟滞约束） ---------- */

static void test_activity_returns_min_immediately(void)
{
    /* MAX 中（t=60000 进入），t=61000 快照到达 → 立即 MIN 且需要切换 */
    app_power_idle_verdict_t v = decide(61000, 61000, 0, MS_MIN, 60000);
    check(v.target == APP_POWER_IDLE_PS_MIN && v.need_switch,
          "activity/snapshot_returns_min", "MAX 中新快照应立即回 MIN 需切换");

    /* MAX 中按键到达：同样立即回 MIN */
    v = decide(62000, 60000, 62000, MS_MIN, 60000);
    check(v.target == APP_POWER_IDLE_PS_MIN && v.need_switch,
          "activity/key_returns_min", "MAX 中按键应立即回 MIN 需切换");

    /* 活动发生在进入 MAX 之后 1ms：仍立即回 MIN（红线不受迟滞约束） */
    v = decide(60001, 60001, 0, MS_MIN, 60000);
    check(v.target == APP_POWER_IDLE_PS_MIN && v.need_switch,
          "activity/overrides_hysteresis", "活动回 MIN 不受迟滞窗约束");

    /* 调用方已回 MIN（编码 -60000）后的活动拍：维持 MIN 且不重复切换 */
    v = decide(61000, 61000, 0, MS_MIN, -60000);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "activity/already_min_no_switch", "已在 MIN 的活动拍不重复切换");
}

/* ---------- 迟滞窗口内不反复（切 MAX 后 60s 内不允许再评估升档） ---------- */

static void test_hysteresis_blocks_rapid_reentry(void)
{
    /* 场景：阈值 10s（Kconfig 下限 1min，宿主域允许更小阈值钉迟滞规则）。
     * t=5000 活动；t=15000 空闲 10s 达标升 MAX（进入时刻 15000）；
     * t=16000 按键 → 立即回 MIN（绝对规则）；
     * t=26000 空闲再次达标（16000+10000），但距上次切 MAX 仅 11s < 60s
     * → 迟滞阻止，保持 MIN 无切换（防抖）。 */
    app_power_idle_verdict_t v = decide(15000, 0, 5000, 10000, 0);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "hysteresis/first_entry_allowed", "首次达标无历史 → 升 MAX");

    v = decide(16000, 16000, 0, 10000, 15000);
    check(v.target == APP_POWER_IDLE_PS_MIN && v.need_switch,
          "hysteresis/activity_exits", "窗口内活动仍立即回 MIN（红线）");

    v = decide(26000, 16000, 0, 10000, -15000);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "hysteresis/reentry_blocked_in_window", "60s 内再达标应被迟滞阻止");

    /* 窗口边缘（恰好 60s，≥ 语义）：允许再评估升档 */
    v = decide(75000, 16000, 0, 10000, -15000);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "hysteresis/reentry_allowed_at_edge", "距上次切 MAX 恰 60s（≥）放行");

    /* 窗口外：正常升档 */
    v = decide(200000, 16000, 0, 10000, -15000);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "hysteresis/reentry_allowed_after_window", "窗口外正常升 MAX");
}

static void test_hysteresis_window_no_flip_flop(void)
{
    /* MAX 保持期内（含迟滞窗与窗外）：只要无活动，反复评估恒 MAX 无切换
     * ——不因重复评估而翻动（空闲时长单调，评估幂等）。 */
    for (int64_t t = 90001; t <= 90001 + 120000; t += 1000) {
        app_power_idle_verdict_t v = decide(t, 30000, 0, MS_MIN, 60000);
        if (!(v.target == APP_POWER_IDLE_PS_MAX && !v.need_switch)) {
            check(0, "hysteresis/hold_no_flip_flop", "MAX 保持期出现翻动");
            return;
        }
    }
    check(1, "hysteresis/hold_no_flip_flop", "120s 保持期逐拍评估零翻动");

    /* 迟滞阻止期同样幂等：阻止窗内恒 MIN 无切换，恰 60s 到点放行 */
    for (int64_t t = 26000; t <= 75000; t += 1000) {
        app_power_idle_verdict_t v = decide(t, 16000, 0, 10000, -15000);
        int in_window = t - 15000 < APP_POWER_IDLE_HYSTERESIS_MS;
        int ok = in_window ? (v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch)
                           : (v.target == APP_POWER_IDLE_PS_MAX && v.need_switch);
        if (!ok) {
            check(0, "hysteresis/block_window_stable", "阻止窗边缘行为不稳定");
            return;
        }
    }
    check(1, "hysteresis/block_window_stable", "阻止窗逐拍稳定、到点放行");
}

/* ---------- 阈值边界（== 恰好触发，≥ 语义） ---------- */

static void test_threshold_boundary_exact(void)
{
    /* idle 恰等于阈值：触发 */
    app_power_idle_verdict_t v = decide(160000, 100000, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "boundary/exact_threshold_triggers", "idle==60s 应恰好触发 MAX");

    /* 差 1ms：不触发 */
    v = decide(159999, 100000, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "boundary/one_ms_short_stays_min", "差 1ms 不触发");

    /* 活动恰在 MAX 进入同一毫秒之后的活动拍（now==rx）：活动优先 */
    v = decide(100000, 100000, 0, MS_MIN, -160000);
    check(v.target == APP_POWER_IDLE_PS_MIN,
          "boundary/fresh_activity_beats_history", "idle==0 未达标优先 MIN");
}

/* ---------- 无效时间戳防御 ---------- */

static void test_invalid_timestamps_defense(void)
{
    app_power_idle_verdict_t v;

    /* 时间倒流（now<0）：一律 MIN */
    v = decide(-1, 100, 100, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/negative_now_min", "now<0 应 MIN 无切换");

    /* 阈值非法（≤0）：一律 MIN */
    v = decide(1000, 0, 0, 0, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/zero_threshold_min", "threshold=0 应 MIN 无切换");
    v = decide(1000, 0, 0, -MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/negative_threshold_min", "threshold<0 应 MIN 无切换");

    /* 双活动时间戳无效（0=从未 / 负值）：无空闲基准 → 不降档 */
    v = decide(1000, 0, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/no_activity_baseline_min", "双 0 活动应 MIN 无切换");
    v = decide(1000, -5, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/negative_activity_min", "负活动时间戳应 MIN 无切换");

    /* 未来活动时间戳（>now）视为无效：另一源有效则取之 */
    v = decide(1000, 2000, 500, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/future_rx_ignored_key_used", "未来 rx 无效，key 500ms 前 → MIN");

    /* 双源一未来一无效：无基准 → MIN */
    v = decide(1000, 5000, 0, MS_MIN, 0);
    check(v.target == APP_POWER_IDLE_PS_MIN && !v.need_switch,
          "invalid/future_rx_only_min", "唯一源为未来时间戳 → MIN");

    /* 未来活动 + 已处于 MAX（编码>0）：保守拉回 MIN */
    v = decide(1000, 5000, 0, MS_MIN, 500);
    check(v.target == APP_POWER_IDLE_PS_MIN && v.need_switch,
          "invalid/future_activity_in_max_pullback", "MAX 中活动源无效仍应保守回 MIN");

    /* 非法档位编码（进入时刻在未来）：档位不可信，但不阻塞空闲主规则
     * ——按「从未进入」处理，达标照常升 MAX（dev_net 档位去重兜底）。 */
    v = decide(100000, 40000, 0, MS_MIN, 200000);
    check(v.target == APP_POWER_IDLE_PS_MAX && v.need_switch,
          "invalid/future_entry_idle_rule_applies", "未来进入时刻不阻塞空闲主规则");

    /* 档位编码 INT64_MIN 防御（取反不回绕为正）：按无效从未处理 */
    v = decide(100000, 0, 100000, MS_MIN, INT64_MIN);
    check(v.target == APP_POWER_IDLE_PS_MIN,
          "invalid/int64_min_encoding_safe", "极值编码不产生升档");
}

/* ---------- main 接线形态（同源编译单元验证） ---------- */

static void test_wiring_shape_compiles_same_source(void)
{
    /* main.c 调用形态：state 编码 >0（MAX 中）/ <0（MIN，带最近进入时刻）
     * 两分支与判决类型可直接互转（编译期即证）。 */
    app_power_idle_verdict_t in_max = decide(90000, 0, 30000, MS_MIN, 60000);
    check(in_max.target == APP_POWER_IDLE_PS_MAX && !in_max.need_switch,
          "shape/max_hold_with_old_key", "MAX 中旧按键（idle 恰 60s）仍保持 MAX 无切换");

    app_power_idle_verdict_t in_min = decide(200000, 130000, 0, MS_MIN, -60000);
    check(in_min.target == APP_POWER_IDLE_PS_MAX && in_min.need_switch,
          "shape/min_state_reevaluates", "MIN 态（曾于 60s 进 MAX）窗口外正常再评估升 MAX");
}

int main(void)
{
    test_below_threshold_stays_min();
    test_over_threshold_switches_max();
    test_activity_returns_min_immediately();
    test_hysteresis_blocks_rapid_reentry();
    test_hysteresis_window_no_flip_flop();
    test_threshold_boundary_exact();
    test_invalid_timestamps_defense();
    test_wiring_shape_compiles_same_source();

    printf("test_power_idle_pure: %d PASS / %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
