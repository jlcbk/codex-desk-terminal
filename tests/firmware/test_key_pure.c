/*
 * test_key_pure.c — P4.5 按键纯状态机主机端测试（A3）
 *
 * 用法：test_key_pure（无参数；真实退出码，任何 FAIL → exit 1）。
 * 真源：docs/DEVELOPMENT_PLAN.md §6（去抖 30ms、长按 800ms、松开时判定、
 * 长按不得再触发短按；短按轮页/长按静音语义由上层映射——本测试只测事件
 * 判定，不测业务映射）、docs/HARDWARE.md §1.2（低有效上拉，active=0）。
 *
 * 覆盖（任务书边界清单）：30ms 内抖动不触发、799/800ms 长按边界、按住期间
 * 不重复触发、松开时判定（按下提交零事件）、长按后不补短按、松开去抖、
 * 上电首样本采纳、上电即按住、高频抖动永不触发。
 */
#include <stdio.h>

#include "cdt_key_pure.h"

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

/* 低有效按键的标准实例（HARDWARE §1.2：active=0，默认去抖/长按） */
static void sm_start(cdt_key_pure_sm_t *sm)
{
    cdt_key_pure_init(sm, 0u, 0u, 0); /* 0 → 默认 30/800 */
}

/* 松开态起步：t=0 采纳 released */
static void sm_start_released(cdt_key_pure_sm_t *sm)
{
    sm_start(sm);
    (void)cdt_key_pure_feed(sm, 1, 0);
}

#define EV_NONE CDT_KEY_PURE_EVT_NONE
#define EV_SHORT CDT_KEY_PURE_EVT_SHORT
#define EV_LONG CDT_KEY_PURE_EVT_LONG

/* ---------- 配置与采纳 ---------- */

static void test_defaults_and_adoption(void)
{
    cdt_key_pure_sm_t sm;
    sm_start(&sm);
    check(sm.debounce_ms == 30u && sm.long_press_ms == 800u,
          "defaults_30_800", "0 传入取 §6 初值 30/800");

    sm_start(&sm);
    check(cdt_key_pure_feed(&sm, 1, 100) == EV_NONE && sm.stable_level == 1,
          "adoption_released_no_event", "首样本 released 采纳无事件");

    sm_start(&sm);
    check(cdt_key_pure_feed(&sm, 0, 100) == EV_NONE && sm.stable_level == 0,
          "adoption_pressed_no_event", "首样本 pressed 采纳无事件（上电即按住）");
}

/* ---------- 短按流程：松开时判定 ---------- */

static void test_short_press_release_judged(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e1;
    cdt_key_pure_event_t e2;
    sm_start_released(&sm);

    e1 = cdt_key_pure_feed(&sm, 0, 100);          /* raw 按下 */
    e2 = cdt_key_pure_feed(&sm, 0, 130);          /* 按下去抖提交 */
    check(e1 == EV_NONE && e2 == EV_NONE,
          "press_commit_no_event", "按下提交不产生事件（松开时判定）");

    e1 = cdt_key_pure_feed(&sm, 0, 200);          /* 按住保持 */
    e2 = cdt_key_pure_feed(&sm, 1, 300);          /* raw 松开（held 200 < 800）*/
    check(e1 == EV_NONE && e2 == EV_NONE, "hold_and_raw_release_no_event",
          "按住与 raw 松开均无事件");
    e1 = cdt_key_pure_feed(&sm, 1, 330);          /* 松开去抖提交 → 判定 */
    check(e1 == EV_SHORT, "release_commit_short", "held 200ms 松开判 SHORT");

    e1 = cdt_key_pure_feed(&sm, 1, 500);          /* 释放保持不再触发 */
    check(e1 == EV_NONE, "released_no_repeat", "松开保持无重复事件");
}

/* ---------- 799/800ms 长按边界（raw 边沿到 raw 边沿） ---------- */

static void test_long_press_boundary(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e;
    int64_t t;

    /* held = 800 → LONG（raw 边沿 100→900；5ms 轮询等价：按下需先去抖提交）*/
    sm_start_released(&sm);
    (void)cdt_key_pure_feed(&sm, 0, 100);
    (void)cdt_key_pure_feed(&sm, 0, 130); /* 按下提交（press_edge=100）*/
    (void)cdt_key_pure_feed(&sm, 1, 900); /* raw 松开边沿：held=800 */
    e = cdt_key_pure_feed(&sm, 1, 930);
    check(e == EV_LONG, "held_800_long", "800ms（含）判 LONG");

    /* held = 799 → SHORT */
    sm_start_released(&sm);
    (void)cdt_key_pure_feed(&sm, 0, 100);
    (void)cdt_key_pure_feed(&sm, 0, 130);
    (void)cdt_key_pure_feed(&sm, 1, 899); /* held=799 */
    e = cdt_key_pure_feed(&sm, 1, 929);
    check(e == EV_SHORT, "held_799_short", "799ms 判 SHORT");

    /* 边界由 raw 边沿决定而非提交时刻：提交在 930 但 held 仍按 900-100 算 */
    sm_start_released(&sm);
    (void)cdt_key_pure_feed(&sm, 0, 100);
    e = EV_NONE;
    for (t = 110; t <= 900; t += 10) { /* 持续喂 pressed */
        e = cdt_key_pure_feed(&sm, 0, t);
    }
    (void)cdt_key_pure_feed(&sm, 1, 905);
    e = cdt_key_pure_feed(&sm, 1, 1000);
    check(e == EV_LONG, "held_edge_not_commit_time", "按住期间持续喂入不影响 held 边沿");
}

/* ---------- 30ms 内抖动不触发 ---------- */

static void test_bounce_within_debounce(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e;
    sm_start_released(&sm);

    (void)cdt_key_pure_feed(&sm, 0, 100); /* raw 按下 */
    (void)cdt_key_pure_feed(&sm, 1, 110); /* 10ms 抖动回 released */
    (void)cdt_key_pure_feed(&sm, 0, 125); /* 再按下：raw 边沿 125 */
    e = cdt_key_pure_feed(&sm, 0, 155);   /* 125+30 去抖提交 */
    check(e == EV_NONE, "bounce_no_spurious_event", "抖动不产生任何事件");

    e = cdt_key_pure_feed(&sm, 1, 400);
    e = cdt_key_pure_feed(&sm, 1, 430);
    check(e == EV_SHORT, "bounce_press_counts_from_stable_raw_edge",
          "抖动后按压边沿=125，held 275 → SHORT");
}

/* ---------- 高频抖动永不去抖提交 ---------- */

static void test_flicker_never_commits(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e = EV_NONE;
    int64_t t;
    int lvl = 0;
    sm_start_released(&sm);
    for (t = 100; t <= 2100; t += 10) { /* 2s 每 10ms 翻转，永不稳定 30ms */
        lvl = (lvl == 0) ? 1 : 0;
        e = cdt_key_pure_feed(&sm, lvl, t);
        if (e != EV_NONE) {
            break;
        }
    }
    check(e == EV_NONE, "flicker_no_event", "30ms 内翻转永远不产生事件");
}

/* ---------- 按住期间不重复触发 + 长按不补短按 ---------- */

static void test_hold_no_repeat_long_no_short(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e = EV_NONE;
    int64_t t;
    int events = 0;
    sm_start_released(&sm);

    (void)cdt_key_pure_feed(&sm, 0, 100);
    for (t = 200; t <= 5100; t += 100) { /* 按住 5s，每 100ms 喂入 */
        e = cdt_key_pure_feed(&sm, 0, t);
        if (e != EV_NONE) {
            events++;
        }
    }
    check(events == 0, "hold_no_repeat", "按住 5s 零事件");

    (void)cdt_key_pure_feed(&sm, 1, 5200); /* raw 松开：held 5100 ≥ 800 */
    e = cdt_key_pure_feed(&sm, 1, 5230);
    check(e == EV_LONG && events == 0, "release_single_long", "松开仅一次 LONG");

    e = cdt_key_pure_feed(&sm, 1, 5400);
    e = cdt_key_pure_feed(&sm, 1, 5600);
    check(e == EV_NONE, "long_no_followup_short", "LONG 后不补发 SHORT（§6）");
}

/* ---------- 长按后再次短按（独立判定） ---------- */

static void test_long_then_short_independent(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e1;
    cdt_key_pure_event_t e2;
    sm_start_released(&sm);

    (void)cdt_key_pure_feed(&sm, 0, 100);
    (void)cdt_key_pure_feed(&sm, 0, 130); /* 按下提交 */
    (void)cdt_key_pure_feed(&sm, 1, 2100); /* held 2000 */
    e1 = cdt_key_pure_feed(&sm, 1, 2130);
    check(e1 == EV_LONG, "first_press_long", "第一按 LONG");

    (void)cdt_key_pure_feed(&sm, 0, 2500);
    (void)cdt_key_pure_feed(&sm, 0, 2530); /* 按下提交 */
    (void)cdt_key_pure_feed(&sm, 1, 2800); /* held 300 */
    e2 = cdt_key_pure_feed(&sm, 1, 2830);
    check(e2 == EV_SHORT, "second_press_short", "第二按独立判 SHORT");
}

/* ---------- 松开去抖：30ms 内回按不拆分按压 ---------- */

static void test_release_debounce(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e;
    sm_start_released(&sm);

    (void)cdt_key_pure_feed(&sm, 0, 100);
    (void)cdt_key_pure_feed(&sm, 0, 130);  /* 按下提交（press_edge=100）*/
    (void)cdt_key_pure_feed(&sm, 1, 1000); /* raw 松开 10ms（未满去抖不提交）*/
    (void)cdt_key_pure_feed(&sm, 0, 1010); /* 回按 */
    e = cdt_key_pure_feed(&sm, 0, 1100);
    check(e == EV_NONE, "release_bounce_still_pressed", "松开抖动未拆分按压");

    (void)cdt_key_pure_feed(&sm, 1, 2000); /* 真松开：held 2000-100=1900 */
    e = cdt_key_pure_feed(&sm, 1, 2030);
    check(e == EV_LONG, "release_bounce_long", "抖动按压连续（边沿仍=100）→ LONG");
}

/* ---------- 快速点按（≥去抖）判 SHORT ---------- */

static void test_quick_tap_short(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e;
    sm_start_released(&sm);
    (void)cdt_key_pure_feed(&sm, 0, 100);  /* raw 按下 */
    (void)cdt_key_pure_feed(&sm, 0, 130);  /* 按下提交 */
    (void)cdt_key_pure_feed(&sm, 1, 140);  /* raw 松开：held 40 */
    e = cdt_key_pure_feed(&sm, 1, 170);
    check(e == EV_SHORT, "quick_tap_40ms_short", "去抖之上、800 之下 → SHORT");
}

/* ---------- 上电即按住：释放判定从采纳时刻起算 ---------- */

static void test_boot_pressed_then_release(void)
{
    cdt_key_pure_sm_t sm;
    cdt_key_pure_event_t e;
    sm_start(&sm);
    (void)cdt_key_pure_feed(&sm, 0, 0);      /* 采纳 pressed，press_edge=0 */
    (void)cdt_key_pure_feed(&sm, 0, 1000);   /* 持续按住无事件 */
    (void)cdt_key_pure_feed(&sm, 1, 1100);   /* raw 松开：held 1100 ≥ 800 */
    e = cdt_key_pure_feed(&sm, 1, 1130);
    check(e == EV_LONG, "boot_pressed_release_long", "上电即按住 1100ms → LONG");
}

int main(void)
{
    test_defaults_and_adoption();
    test_short_press_release_judged();
    test_long_press_boundary();
    test_bounce_within_debounce();
    test_flicker_never_commits();
    test_hold_no_repeat_long_no_short();
    test_long_then_short_independent();
    test_release_debounce();
    test_quick_tap_short();
    test_boot_pressed_then_release();

    printf("test_key_pure: %d PASS / %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
