/*
 * cdt_power_exec_pure.c — §7.3 停止顺序纯逻辑实现（P5.3，A3）
 *
 * 规则真源见 cdt_power_exec_pure.h 文件头。本文件零平台头：只依赖
 * stdint/stdbool/stddef 与 shared/power/cdt_power.h 的类型。
 */
#include "cdt_power_exec_pure.h"

/* ---------------- 轨迹 ---------------- */

void cdt_pexec_trace_reset(cdt_pexec_trace_t *t)
{
    if (t == NULL) {
        return;
    }
    t->len = 0;
    t->overflow = false;
}

void cdt_pexec_trace_push(cdt_pexec_trace_t *t, cdt_pexec_step_t step)
{
    if (t == NULL) {
        return;
    }
    if (t->len >= CDT_PEXEC_TRACE_MAX) {
        t->overflow = true;
        return;
    }
    t->steps[t->len] = step;
    t->len++;
}

/* ---------------- 顺序编排状态机 ---------------- */

void cdt_pexec_seq_init(cdt_pexec_seq_t *s, uint32_t ff_timeout_ms)
{
    if (s == NULL) {
        return;
    }
    s->ff_timeout_ms = ff_timeout_ms;
    s->next_index = (uint8_t)CDT_PEXEC_STEP_BLOCK_NEW_UPDATES;
    s->ff_flushed = false;
    s->ff_timed_out = false;
    s->ff_abandoned = false;
    s->done_count = 0;
}

cdt_pexec_step_t cdt_pexec_seq_current(const cdt_pexec_seq_t *s)
{
    if (s == NULL || s->next_index < (uint8_t)CDT_PEXEC_STEP_BLOCK_NEW_UPDATES ||
        s->next_index > (uint8_t)CDT_PEXEC_STEP_ENTER_DEEP_SLEEP) {
        return CDT_PEXEC_STEP_NONE;
    }
    return (cdt_pexec_step_t)s->next_index;
}

bool cdt_pexec_seq_terminal(const cdt_pexec_seq_t *s)
{
    return cdt_pexec_seq_current(s) == CDT_PEXEC_STEP_NONE;
}

bool cdt_pexec_seq_step_done(cdt_pexec_seq_t *s)
{
    if (s == NULL || cdt_pexec_seq_terminal(s)) {
        return false;
    }
    /* §7.3 步骤②必须显式收敛（完成/超时/放弃三选一），不允许跳过 */
    if (s->next_index == (uint8_t)CDT_PEXEC_STEP_FINAL_FRAME) {
        return false;
    }
    s->next_index++;
    s->done_count++;
    return true;
}

void cdt_pexec_seq_final_frame_settled(cdt_pexec_seq_t *s,
                                       bool flushed, bool timed_out, bool abandoned)
{
    if (s == NULL || s->next_index != (uint8_t)CDT_PEXEC_STEP_FINAL_FRAME) {
        return;
    }
    s->ff_flushed = flushed;
    s->ff_timed_out = timed_out;
    s->ff_abandoned = abandoned;
    s->next_index = (uint8_t)CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO;
    s->done_count++;
}

/* ---------------- 末帧超时判定与等待 ---------------- */

bool cdt_pexec_timeout_due(int64_t started_ms, int64_t now_ms, uint32_t timeout_ms)
{
    /* 与 P5.1 FSM 同语义：同一毫秒恰好到期即触发；时钟倒退（now<started）
     * 不触发（单调时钟下不应发生，防御处理）。 */
    return (now_ms - started_ms) >= (int64_t)timeout_ms;
}

void cdt_pexec_final_frame_wait(const cdt_pexec_wait_ops_t *ops, int64_t started_ms,
                                uint32_t timeout_ms,
                                bool *flushed, bool *timed_out, bool *abandoned)
{
    bool f = false;
    bool t = false;
    bool a = false;

    if (ops == NULL || ops->poll_flush == NULL) {
        /* 无末帧句柄：§7.3 "完成/放弃末帧"的放弃路径，直接进入停机下一段 */
        a = true;
    } else if (ops->poll_flush(ops->user)) {
        f = true;
    } else if (ops->wait_ms == NULL || ops->now_ms == NULL) {
        /* 无等待/无时钟注入：只做一次到期判定（now=started），不忙等 */
        t = cdt_pexec_timeout_due(started_ms, started_ms, timeout_ms);
    } else {
        uint64_t iter = 0;
        const uint64_t cap = (uint64_t)timeout_ms + (uint64_t)CDT_PEXEC_WAIT_ITER_SLACK;
        for (;;) {
            int64_t now = ops->now_ms(ops->user);
            if (cdt_pexec_timeout_due(started_ms, now, timeout_ms)) {
                t = true; /* §7.2：超时也要休眠 */
                break;
            }
            if (iter >= cap) {
                /* 时钟不前进的防御上限（正常注入 1ms 时钟永远先因 due 退出）*/
                t = true;
                break;
            }
            ops->wait_ms(ops->user, CDT_PEXEC_WAIT_GRANULARITY_MS);
            iter++;
            if (ops->poll_flush(ops->user)) {
                f = true;
                break;
            }
        }
    }

    if (flushed != NULL) {
        *flushed = f;
    }
    if (timed_out != NULL) {
        *timed_out = t;
    }
    if (abandoned != NULL) {
        *abandoned = a;
    }
}

/* ---------------- RTC 低压原因标记 ---------------- */

uint8_t cdt_pexec_reason_encode(bool low_battery)
{
    return (uint8_t)(CDT_PEXEC_REASON_MAGIC | CDT_PEXEC_REASON_VER |
                     (low_battery ? CDT_PEXEC_REASON_LOW_BIT : 0u));
}

bool cdt_pexec_reason_decode(uint8_t raw, bool *low_battery_out)
{
    if (((uint8_t)raw & (uint8_t)CDT_PEXEC_REASON_MAGIC_MASK) !=
        (uint8_t)CDT_PEXEC_REASON_MAGIC) {
        return false;
    }
    if (((uint8_t)raw & (uint8_t)CDT_PEXEC_REASON_VER_MASK) !=
        (uint8_t)CDT_PEXEC_REASON_VER) {
        return false;
    }
    if (low_battery_out != NULL) {
        *low_battery_out = ((uint8_t)raw & (uint8_t)CDT_PEXEC_REASON_LOW_BIT) != 0u;
    }
    return true;
}

bool cdt_pexec_reason_write(const cdt_pexec_reason_io_t *io, bool low_battery)
{
    if (io == NULL || io->write == NULL) {
        return false;
    }
    return io->write(io->user, (uint32_t)cdt_pexec_reason_encode(low_battery));
}

bool cdt_pexec_reason_read(const cdt_pexec_reason_io_t *io, bool *low_battery_out)
{
    uint32_t raw = 0u;
    if (io == NULL || io->read == NULL) {
        return false;
    }
    if (!io->read(io->user, &raw)) {
        return false;
    }
    if ((raw & ~(uint32_t)0xFFu) != 0u) {
        return false; /* 脏高位：按无效标记处理（§7.3 无效读数走故障/首样本策略）*/
    }
    return cdt_pexec_reason_decode((uint8_t)(raw & 0xFFu), low_battery_out);
}

void cdt_pexec_reason_clear(const cdt_pexec_reason_io_t *io)
{
    if (io == NULL) {
        return;
    }
    if (io->clear != NULL) {
        io->clear(io->user);
        return;
    }
    if (io->write != NULL) {
        (void)io->write(io->user, 0u);
    }
}

bool cdt_pexec_wake_gate_allow_radio(const cdt_pexec_reason_io_t *io,
                                     bool *low_wake_hint_out)
{
    bool low = false;
    bool valid = cdt_pexec_reason_read(io, &low);
    bool deny = valid && low;
    if (low_wake_hint_out != NULL) {
        *low_wake_hint_out = deny; /* 仅有效低压标记向 FSM 提供 wake_low_hint */
    }
    return !deny;
}
