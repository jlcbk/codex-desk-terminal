/*
 * cdt_power.c — 共享纯 Power FSM 实现（P5.1，A3）
 *
 * 规则真源：docs/DEVELOPMENT_PLAN.md §7.1/§7.2/§7.3。要点：
 *   - §7.2 连续定义：任一有效样本 > critical_mv 重置 critical 计时；连续有效样本
 *     间隔须 ≤ sample_gap_max_ms（2000ms），间隔超限转采样故障检查，缺测时间不
 *     计入持续低压；critical 成立后锁存，不因电压反弹撤销。
 *   - §7.2 迟滞：LOW_WARN 进入 ≤ low_enter_mv；退出需严格大于 low_exit_mv 且
 *     连续稳定 stable_ms（10s）。
 *   - §7.3 BOOT_CHECK：开机/深睡唤醒必经；先 ADC 后无线；低压唤醒（RTC 低压
 *     原因或首次样本 ≤ critical_mv）要求 recovery（≥ recovery_mv 稳定 10s）才
 *     ACTIVE，否则不开无线直接回 SLEEP_PREP；无效读数走 §7.1 故障策略。
 *   - §7.1 故障：连续 fault_fail_count（3）次无效样本 → BATTERY_FAULT（关闭高
 *     功耗提示）；fault_grace_ms（10s）仍不可恢复 → 受控休眠（经 §7.3 CRITICAL
 *     故障安全路径）。
 *   - §7.3 CRITICAL → SLEEP_PREP：锁存后保留一步 CRITICAL（供 runtime/UI 显示
 *     critical），下一输入进入 SLEEP_PREP；末帧完成或超时 final_frame_timeout_ms
 *     → DEEP_SLEEP（正常情况下 critical 成立后 2s 内）。
 *   - ACTIVE ↔ CONNECTED_IDLE ↔ OFFLINE_LIGHT_SLEEP 本任务只落状态与占位动作，
 *     无线策略实测归 P5.2。
 *
 * 时间全部单调毫秒 int64、电压毫伏整数比较，无浮点、无平台依赖、无 IO。
 */
#include "cdt_power.h"

/* ---------------- 参数与初始化 ---------------- */

void cdt_power_params_init(cdt_power_params_t *params)
{
    if (params == NULL) {
        return;
    }
    /* §7.2 表初值 */
    params->low_enter_mv = 3700u;
    params->low_exit_mv = 3750u;
    params->critical_mv = 3600u;
    params->recovery_mv = 3700u;
    params->critical_hold_ms = 30000u;
    params->stable_ms = 10000u;
    params->final_frame_timeout_ms = 1000u;
    /* §7.2 连续定义 / §7.1 故障策略初值 */
    params->sample_gap_max_ms = 2000u;
    params->fault_fail_count = 3u;
    params->fault_grace_ms = 10000u;
    /* §7.1 有效范围初值 */
    params->valid_min_mv = 2500u;
    params->valid_max_mv = 4500u;
    /* §7.1 校准默认恒等（P4.4 定标后注入）*/
    params->cal_offset_mv = 0;
    params->cal_gain_ppm = 1000000;
}

void cdt_power_init(cdt_power_fsm_t *fsm, const cdt_power_params_t *params,
                    bool wake_low_hint)
{
    if (fsm == NULL) {
        return;
    }
    /* 清零全部计时/标志，状态必经 BOOT_CHECK（§7.3）*/
    {
        size_t n = sizeof(*fsm);
        unsigned char *p = (unsigned char *)fsm;
        while (n > 0) {
            p[--n] = 0;
        }
    }
    if (params != NULL) {
        fsm->params = *params;
    } else {
        cdt_power_params_init(&fsm->params);
    }
    fsm->wake_low_hint = wake_low_hint;
    fsm->state = CDT_POWER_BOOT_CHECK;
}

int32_t cdt_power_effective_mv(const cdt_power_params_t *params, uint16_t raw_mv)
{
    int64_t v;
    if (params == NULL) {
        return (int32_t)raw_mv;
    }
    v = (int64_t)raw_mv * (int64_t)params->cal_gain_ppm / (int64_t)1000000 +
        (int64_t)params->cal_offset_mv;
    return (int32_t)v;
}

/* ---------------- 内部辅助 ---------------- */

/* 故障策略生效域：尚未进入休眠流程的运行态（§7.3 BOOT_CHECK 行"无效读数走故障策略"）*/
static bool state_in_fault_domain(cdt_power_state_t s)
{
    return s == CDT_POWER_BOOT_CHECK || s == CDT_POWER_ACTIVE ||
           s == CDT_POWER_CONNECTED_IDLE || s == CDT_POWER_OFFLINE_LIGHT_SLEEP ||
           s == CDT_POWER_LOW_WARN;
}

/* §7.2：CRITICAL 成立（30s 连续低压）或故障受控休眠：锁存并请求冻结末帧。
 * 状态停在 CRITICAL 一步，下一次 step 进入 SLEEP_PREP（见 cdt_power_step）。 */
static void enter_critical_latched(cdt_power_fsm_t *fsm, int64_t now_ms,
                                   cdt_power_action_t *act)
{
    if (fsm->critical_latched) {
        return; /* 已锁存不重复（§7.2 锁存语义）*/
    }
    fsm->critical_latched = true;
    fsm->crit_running = false;
    fsm->lowexit_running = false;
    fsm->recov_running = false;
    fsm->final_frame_pending = true;
    fsm->final_frame_done = false;
    fsm->final_frame_timed_out = false;
    fsm->sleep_prep_since_ms = now_ms;
    fsm->state = CDT_POWER_CRITICAL;
    *act |= CDT_POWER_ACT_ENTER_CRITICAL;
}

/* §7.3 BOOT_CHECK 行：低压唤醒未满足 recovery → 不开无线直接回 SLEEP_PREP。
 * 锁存休眠决定（P5.3 验收：低压反弹不反复启动）。 */
static void boot_no_recovery_sleep(cdt_power_fsm_t *fsm, int64_t now_ms,
                                   cdt_power_action_t *act)
{
    fsm->critical_latched = true;
    fsm->crit_running = false;
    fsm->lowexit_running = false;
    fsm->recov_running = false;
    fsm->final_frame_pending = true;
    fsm->final_frame_done = false;
    fsm->final_frame_timed_out = false;
    fsm->sleep_prep_since_ms = now_ms;
    fsm->state = CDT_POWER_SLEEP_PREP;
    *act |= CDT_POWER_ACT_BEGIN_SLEEP_PREP;
}

/* SLEEP_PREP 内完成末帧（flush 事件）→ DEEP_SLEEP 现在可入（§7.3）*/
static void finish_final_frame(cdt_power_fsm_t *fsm, cdt_power_action_t *act)
{
    fsm->final_frame_pending = false;
    fsm->final_frame_done = true;
    fsm->state = CDT_POWER_DEEP_SLEEP;
    *act |= CDT_POWER_ACT_DEEP_SLEEP_READY;
}

/* 无效样本（valid=false 或 §7.1 范围外）：计入连续失败（§7.1）*/
static void handle_invalid_sample(cdt_power_fsm_t *fsm, int64_t now_ms,
                                  cdt_power_action_t *act)
{
    if (!state_in_fault_domain(fsm->state)) {
        return; /* 休眠流程中的无效读数不再改变路径 */
    }
    if (fsm->invalid_streak < 255u) {
        fsm->invalid_streak++;
    }
    if (!fsm->battery_fault && fsm->invalid_streak >= fsm->params.fault_fail_count) {
        /* §7.1：连续 3 次失败进入 BATTERY_FAULT：关闭高功耗活动并提示 */
        fsm->battery_fault = true;
        fsm->fault_since_ms = now_ms;
        *act |= CDT_POWER_ACT_BATTERY_FAULT;
    }
}

/* BOOT_CHECK 的有效样本：启动判定 + recovery watch（§7.3 BOOT_CHECK 行）*/
static void handle_boot_sample(cdt_power_fsm_t *fsm, int32_t mv, int64_t at_ms,
                               cdt_power_action_t *act)
{
    const cdt_power_params_t *p = &fsm->params;

    if (!fsm->boot_decided) {
        fsm->boot_decided = true;
        if (!fsm->wake_low_hint && mv > (int32_t)p->critical_mv) {
            /* 正常启动健康电压直接运行；先 ADC 后无线，允许开无线 */
            fsm->boot_recovery_required = false;
            *act |= CDT_POWER_ACT_ALLOW_RADIO_START;
            if (mv <= (int32_t)p->low_enter_mv) {
                fsm->state = CDT_POWER_LOW_WARN;
                *act |= CDT_POWER_ACT_ENTER_LOW_WARN;
            } else {
                fsm->state = CDT_POWER_ACTIVE;
            }
            return;
        }
        /* 低压唤醒（RTC 低压原因或首次样本 ≤ critical_mv）：要求 recovery，
         * 不开无线（§7.3）；继续用本样本走下方 watch 判定。 */
        fsm->boot_recovery_required = true;
        *act |= CDT_POWER_ACT_BLOCK_RADIO_START;
    }

    if (!fsm->boot_recovery_required) {
        return;
    }
    if (mv <= (int32_t)p->critical_mv) {
        /* recovery 未满足（仍 ≤ critical_mv）→ 不开无线直接回 SLEEP_PREP（§7.3）*/
        boot_no_recovery_sleep(fsm, at_ms, act);
        return;
    }
    if (mv >= (int32_t)p->recovery_mv) {
        /* ≥ recovery_mv：连续稳定 stable_ms 后恢复（§7.2，到期判定在 step）*/
        if (!fsm->recov_running) {
            fsm->recov_running = true;
            fsm->recov_start_ms = at_ms;
        }
    } else {
        /* critical_mv < mv < recovery_mv：不给恢复计时 */
        fsm->recov_running = false;
    }
}

/* 有效样本处理（已完成校准与范围检查）*/
static cdt_power_action_t handle_valid_sample(cdt_power_fsm_t *fsm, int32_t mv,
                                              int64_t at_ms)
{
    cdt_power_action_t act = CDT_POWER_ACT_NONE;
    const cdt_power_params_t *p = &fsm->params;

    /* §7.2 连续定义（间隔须 ≤ sample_gap_max_ms=2s）是**临界持续低压累计期**
     * （§7.1 近阈值 1Hz 快采）的连续性判据：仅当任一连续性计时在跑
     * （crit/lowexit/recov——这些计时只在近阈值区间运行，彼时固件按 §7.1 必为
     * 1Hz 节奏）时，间隔超限（或乱序）才构成缺测 → 重置连续计时，缺测时间
     * 不计入持续低压/稳定窗口（§7.2）。
     * 常规 10s 节奏（§7.1；电压远离阈值、无任何计时在跑）是正常采样节奏而非
     * 缺测：ACTIVE 常规模式不得触发本动作。真机回归证据（A4 断连重连修复
     * 任务，2026-09-11）：修复前每 10s 拍误报一次 SAMPLE_GAP_RESET
     * （artifacts/board/integration/reconnect_test.log）；主机测试
     * test_sample_gap_normal_cadence_no_reset 钉死该语义。 */
    if (fsm->have_last_valid) {
        int64_t gap = at_ms - fsm->last_valid_at_ms;
        bool continuity_armed = fsm->crit_running || fsm->lowexit_running ||
                                fsm->recov_running;
        if (continuity_armed && (gap < 0 || gap > (int64_t)p->sample_gap_max_ms)) {
            act |= CDT_POWER_ACT_SAMPLE_GAP_RESET;
            fsm->gap_reset_count++;
            fsm->crit_running = false;
            fsm->lowexit_running = false;
            fsm->recov_running = false;
        }
    }
    fsm->have_last_valid = true;
    fsm->last_valid_at_ms = at_ms;

    /* §7.1：故障期内出现有效样本 → 恢复 */
    if (fsm->battery_fault) {
        fsm->battery_fault = false;
        act |= CDT_POWER_ACT_BATTERY_FAULT_RECOVERED;
    }
    fsm->invalid_streak = 0;

    /* §7.2：任一有效样本 > critical_mv 重置 critical 计时 */
    if (mv > (int32_t)p->critical_mv) {
        fsm->crit_running = false;
    } else if (!fsm->crit_running) {
        fsm->crit_running = true;
        fsm->crit_start_ms = at_ms;
    }

    switch (fsm->state) {
    case CDT_POWER_BOOT_CHECK:
        handle_boot_sample(fsm, mv, at_ms, &act);
        break;

    case CDT_POWER_LOW_WARN:
        /* §7.2：退出需 > low_exit_mv（严格大于）连续稳定 stable_ms；
         * ≤ low_exit_mv（含迟滞带 3601–3750）重置退出计时 */
        if (mv > (int32_t)p->low_exit_mv) {
            if (!fsm->lowexit_running) {
                fsm->lowexit_running = true;
                fsm->lowexit_start_ms = at_ms;
            }
        } else {
            fsm->lowexit_running = false;
        }
        break;

    case CDT_POWER_ACTIVE:
    case CDT_POWER_CONNECTED_IDLE:
    case CDT_POWER_OFFLINE_LIGHT_SLEEP:
        /* §7.3 LOW_WARN 行：≤ low_enter_mv 进入警告，业务继续 */
        if (mv <= (int32_t)p->low_enter_mv) {
            fsm->state = CDT_POWER_LOW_WARN;
            fsm->lowexit_running = false;
            act |= CDT_POWER_ACT_ENTER_LOW_WARN;
        }
        break;

    case CDT_POWER_CRITICAL:
    case CDT_POWER_SLEEP_PREP:
    case CDT_POWER_DEEP_SLEEP:
    default:
        /* 已锁存/休眠中：样本不改变路径（§7.2 锁存不因反弹撤销）*/
        break;
    }
    return act;
}

static cdt_power_action_t handle_sample(cdt_power_fsm_t *fsm,
                                        const cdt_power_sample_t *s)
{
    cdt_power_action_t act;
    int32_t mv;
    if (fsm->state == CDT_POWER_DEEP_SLEEP) {
        return CDT_POWER_ACT_NONE; /* 终态：PWR 重新上电走 cdt_power_init（HARDWARE §4）*/
    }
    act = CDT_POWER_ACT_NONE;
    if (!s->valid) {
        handle_invalid_sample(fsm, s->at_ms, &act);
        return act;
    }
    mv = cdt_power_effective_mv(&fsm->params, s->battery_mv);
    /* §7.1：范围外 → unknown，按失败样本计 */
    if (mv < (int32_t)fsm->params.valid_min_mv || mv > (int32_t)fsm->params.valid_max_mv) {
        handle_invalid_sample(fsm, s->at_ms, &act);
        return act;
    }
    return handle_valid_sample(fsm, mv, s->at_ms);
}

/* 事件处理（§7.3 状态转移表；LOW_WARN 中业务继续、不改电池域状态）*/
static cdt_power_action_t handle_event(cdt_power_fsm_t *fsm, cdt_power_event_t ev)
{
    cdt_power_action_t act = CDT_POWER_ACT_NONE;
    if (ev == CDT_POWER_EVT_NONE) {
        return act;
    }
    switch (fsm->state) {
    case CDT_POWER_ACTIVE:
        /* §7.3 CONNECTED_IDLE 行：无脏画面/无任务（占位，P5.2 定实测策略）*/
        if (ev == CDT_POWER_EVT_IDLE_REACHED) {
            fsm->state = CDT_POWER_CONNECTED_IDLE;
            act |= CDT_POWER_ACT_ENTER_CONNECTED_IDLE;
        } else if (ev == CDT_POWER_EVT_OFFLINE_SLEEP) {
            fsm->state = CDT_POWER_OFFLINE_LIGHT_SLEEP;
            act |= CDT_POWER_ACT_ENTER_OFFLINE_LIGHT_SLEEP;
        }
        break;

    case CDT_POWER_CONNECTED_IDLE:
        if (ev == CDT_POWER_EVT_ACTIVITY || ev == CDT_POWER_EVT_KEY_WAKE) {
            fsm->state = CDT_POWER_ACTIVE;
            act |= CDT_POWER_ACT_ENTER_ACTIVE;
        } else if (ev == CDT_POWER_EVT_OFFLINE_SLEEP) {
            fsm->state = CDT_POWER_OFFLINE_LIGHT_SLEEP;
            act |= CDT_POWER_ACT_ENTER_OFFLINE_LIGHT_SLEEP;
        }
        break;

    case CDT_POWER_OFFLINE_LIGHT_SLEEP:
        /* §7.3 OFFLINE_LIGHT_SLEEP 行：定时/按键唤醒后重连拿全量（占位）*/
        if (ev == CDT_POWER_EVT_ACTIVITY || ev == CDT_POWER_EVT_KEY_WAKE) {
            fsm->state = CDT_POWER_ACTIVE;
            act |= CDT_POWER_ACT_ENTER_ACTIVE;
        }
        break;

    case CDT_POWER_SLEEP_PREP:
        /* §7.3 SLEEP_PREP 行：等待 flush ≤1s，完成即休眠 */
        if (ev == CDT_POWER_EVT_FINAL_FRAME_DONE && fsm->final_frame_pending) {
            finish_final_frame(fsm, &act);
        }
        break;

    case CDT_POWER_BOOT_CHECK:
    case CDT_POWER_LOW_WARN:
    case CDT_POWER_CRITICAL:
    case CDT_POWER_DEEP_SLEEP:
    default:
        /* BOOT_CHECK：活动不改变启动判定；LOW_WARN：业务继续；
         * CRITICAL 的末帧事件在进入 SLEEP_PREP 后处理；DEEP_SLEEP 终态。 */
        break;
    }
    return act;
}

/* 超时/保持检查（全部用单调 now_ms；§7.2"30 秒条件与阈值都用单调时钟"）*/
static cdt_power_action_t check_timeouts(cdt_power_fsm_t *fsm, int64_t now_ms)
{
    cdt_power_action_t act = CDT_POWER_ACT_NONE;
    const cdt_power_params_t *p = &fsm->params;

    /* §7.1：BATTERY_FAULT 10s 仍不可恢复 → 受控休眠（经 CRITICAL 故障安全路径）*/
    if (fsm->battery_fault && state_in_fault_domain(fsm->state) &&
        now_ms - fsm->fault_since_ms >= (int64_t)p->fault_grace_ms) {
        act |= CDT_POWER_ACT_CONTROLLED_SLEEP;
        enter_critical_latched(fsm, now_ms, &act);
    }

    /* §7.2：连续有效低压保持 critical_hold_ms → CRITICAL 锁存 */
    if (!fsm->critical_latched && fsm->crit_running &&
        now_ms - fsm->crit_start_ms >= (int64_t)p->critical_hold_ms) {
        enter_critical_latched(fsm, now_ms, &act);
    }

    /* §7.2：recovery ≥ recovery_mv 连续稳定 stable_ms → ACTIVE 并允许开无线 */
    if (fsm->state == CDT_POWER_BOOT_CHECK && fsm->boot_recovery_required &&
        fsm->recov_running &&
        now_ms - fsm->recov_start_ms >= (int64_t)p->stable_ms) {
        fsm->recov_running = false;
        fsm->boot_recovery_required = false;
        fsm->state = CDT_POWER_ACTIVE;
        act |= CDT_POWER_ACT_ALLOW_RADIO_START;
    }

    /* §7.2：LOW_WARN 退出 > low_exit_mv 连续稳定 stable_ms */
    if (fsm->state == CDT_POWER_LOW_WARN && fsm->lowexit_running &&
        now_ms - fsm->lowexit_start_ms >= (int64_t)p->stable_ms) {
        fsm->lowexit_running = false;
        fsm->state = CDT_POWER_ACTIVE;
        act |= CDT_POWER_ACT_EXIT_LOW_WARN;
    }

    /* §7.3 SLEEP_PREP 行 / §7.2 final_frame_timeout_ms：末帧超时也必须休眠 */
    if (fsm->state == CDT_POWER_SLEEP_PREP && fsm->final_frame_pending &&
        now_ms - fsm->sleep_prep_since_ms >= (int64_t)p->final_frame_timeout_ms) {
        fsm->final_frame_pending = false;
        fsm->final_frame_timed_out = true;
        fsm->state = CDT_POWER_DEEP_SLEEP;
        act |= CDT_POWER_ACT_FINAL_FRAME_TIMEOUT | CDT_POWER_ACT_DEEP_SLEEP_READY;
    }
    return act;
}

/* ---------------- 对外主入口 ---------------- */

cdt_power_action_t cdt_power_step(cdt_power_fsm_t *fsm,
                                  const cdt_power_input_t *in, int64_t now_ms)
{
    cdt_power_action_t act = CDT_POWER_ACT_NONE;
    if (fsm == NULL) {
        return CDT_POWER_ACT_NONE;
    }

    /* [A] CRITICAL 观察步：锁存后保留一步 critical（runtime/UI 可显示），
     * 下一输入进入 SLEEP_PREP（§7.3 CRITICAL → SLEEP_PREP）。 */
    if (fsm->state == CDT_POWER_CRITICAL) {
        fsm->state = CDT_POWER_SLEEP_PREP;
        fsm->sleep_prep_since_ms = now_ms;
        act |= CDT_POWER_ACT_BEGIN_SLEEP_PREP;
    }

    /* [B] 输入处理（样本优先级高于普通事件；本地低压不经过网络队列，§5）*/
    if (in != NULL) {
        if (in->kind == CDT_POWER_IN_SAMPLE) {
            act |= handle_sample(fsm, &in->sample);
        } else if (in->kind == CDT_POWER_IN_EVENT) {
            act |= handle_event(fsm, in->event);
        }
    }

    /* [C] 超时/保持检查（同一 now_ms 恰好到期即触发）*/
    act |= check_timeouts(fsm, now_ms);
    return act;
}

/* ---------------- 诊断辅助（纯函数，无 IO）---------------- */

/* 追加字符串到 buf[used..cap)，超长截断（无 stdio 依赖）*/
static void append_str(char *buf, size_t cap, size_t *used, const char *s)
{
    while (*s != '\0' && *used + 1 < cap) {
        buf[(*used)++] = *s++;
    }
    if (cap > 0) {
        buf[*used] = '\0';
    }
}

const char *cdt_power_state_str(cdt_power_state_t state)
{
    switch (state) {
    case CDT_POWER_BOOT_CHECK:
        return "BOOT_CHECK";
    case CDT_POWER_ACTIVE:
        return "ACTIVE";
    case CDT_POWER_CONNECTED_IDLE:
        return "CONNECTED_IDLE";
    case CDT_POWER_OFFLINE_LIGHT_SLEEP:
        return "OFFLINE_LIGHT_SLEEP";
    case CDT_POWER_LOW_WARN:
        return "LOW_WARN";
    case CDT_POWER_CRITICAL:
        return "CRITICAL";
    case CDT_POWER_SLEEP_PREP:
        return "SLEEP_PREP";
    case CDT_POWER_DEEP_SLEEP:
        return "DEEP_SLEEP";
    case CDT_POWER_INVALID:
    default:
        return "INVALID";
    }
}

const char *cdt_power_actions_str(char *buf, size_t cap, cdt_power_action_t actions)
{
    static const struct {
        cdt_power_action_t bit;
        const char *name;
    } tab[] = {
        { CDT_POWER_ACT_ENTER_LOW_WARN, "ENTER_LOW_WARN" },
        { CDT_POWER_ACT_EXIT_LOW_WARN, "EXIT_LOW_WARN" },
        { CDT_POWER_ACT_ENTER_CRITICAL, "ENTER_CRITICAL" },
        { CDT_POWER_ACT_BEGIN_SLEEP_PREP, "BEGIN_SLEEP_PREP" },
        { CDT_POWER_ACT_DEEP_SLEEP_READY, "DEEP_SLEEP_READY" },
        { CDT_POWER_ACT_FINAL_FRAME_TIMEOUT, "FINAL_FRAME_TIMEOUT" },
        { CDT_POWER_ACT_BATTERY_FAULT, "BATTERY_FAULT" },
        { CDT_POWER_ACT_BATTERY_FAULT_RECOVERED, "FAULT_RECOVERED" },
        { CDT_POWER_ACT_CONTROLLED_SLEEP, "CONTROLLED_SLEEP" },
        { CDT_POWER_ACT_BLOCK_RADIO_START, "BLOCK_RADIO_START" },
        { CDT_POWER_ACT_ALLOW_RADIO_START, "ALLOW_RADIO_START" },
        { CDT_POWER_ACT_SAMPLE_GAP_RESET, "SAMPLE_GAP_RESET" },
        { CDT_POWER_ACT_ENTER_ACTIVE, "ENTER_ACTIVE" },
        { CDT_POWER_ACT_ENTER_CONNECTED_IDLE, "ENTER_CONNECTED_IDLE" },
        { CDT_POWER_ACT_ENTER_OFFLINE_LIGHT_SLEEP, "ENTER_OFFLINE_LIGHT_SLEEP" }
    };
    size_t i;
    size_t used = 0;
    if (buf == NULL || cap == 0) {
        return buf ? buf : "";
    }
    buf[0] = '\0';
    if (actions == CDT_POWER_ACT_NONE) {
        append_str(buf, cap, &used, "NONE");
        return buf;
    }
    for (i = 0; i < sizeof(tab) / sizeof(tab[0]); i++) {
        if ((actions & tab[i].bit) != 0) {
            if (used > 0) {
                append_str(buf, cap, &used, "|");
            }
            append_str(buf, cap, &used, tab[i].name);
        }
    }
    return buf;
}
