/*
 * cdt_key_pure.c — 按键去抖/长短按纯状态机实现（P4.5 固件侧，A3）
 *
 * 语义精确定义见 cdt_key_pure.h 文件头；§6 要点：去抖 30ms、长按 800ms、
 * 松开时判定、长按不得再触发短按、按住期间不重复触发。
 */
#include "cdt_key_pure.h"

void cdt_key_pure_init(cdt_key_pure_sm_t *sm, uint32_t debounce_ms,
                       uint32_t long_press_ms, int active_level)
{
    if (sm == NULL) {
        return;
    }
    sm->debounce_ms = (debounce_ms == 0u) ? CDT_KEY_DEBOUNCE_MS_DEFAULT : debounce_ms;
    sm->long_press_ms = (long_press_ms == 0u) ? CDT_KEY_LONG_PRESS_MS_DEFAULT
                                              : long_press_ms;
    sm->active_level = active_level;
    sm->raw_level = 0;
    sm->raw_since_ms = 0;
    sm->stable_level = -1; /* 首样本采纳态 */
    sm->press_edge_ms = 0;
}

cdt_key_pure_event_t cdt_key_pure_feed(cdt_key_pure_sm_t *sm, int level,
                                       int64_t now_ms)
{
    cdt_key_pure_event_t ev = CDT_KEY_PURE_EVT_NONE;

    if (sm == NULL) {
        return CDT_KEY_PURE_EVT_NONE;
    }

    if (sm->stable_level < 0) {
        /* 首样本采纳（上电电平，可能就是按住）：不产生事件。
         * 若上电即按住，按住起点记为首样本时刻（确定性语义）。 */
        sm->stable_level = level;
        sm->raw_level = level;
        sm->raw_since_ms = now_ms;
        if (level == sm->active_level) {
            sm->press_edge_ms = now_ms;
        }
        return CDT_KEY_PURE_EVT_NONE;
    }

    /* raw 电平变化：刷新 raw 边沿时刻；同电平重复喂入不动计时 */
    if (level != sm->raw_level) {
        sm->raw_level = level;
        sm->raw_since_ms = now_ms;
    }

    /* 电平去抖：raw 连续保持满 debounce_ms 且与 stable 不同 → 提交 */
    if (sm->raw_level != sm->stable_level &&
        now_ms - sm->raw_since_ms >= (int64_t)sm->debounce_ms) {
        int prev = sm->stable_level;
        sm->stable_level = sm->raw_level;
        if (sm->stable_level == sm->active_level) {
            /* 按下提交：只记录 raw 按下边沿，不产生事件（§6 松开时判定） */
            sm->press_edge_ms = sm->raw_since_ms;
        } else if (prev == sm->active_level) {
            /* 松开提交：一次性判定 LONG/SHORT（§6：长按不得再触发短按） */
            int64_t held = sm->raw_since_ms - sm->press_edge_ms;
            ev = (held >= (int64_t)sm->long_press_ms) ? CDT_KEY_PURE_EVT_LONG
                                                      : CDT_KEY_PURE_EVT_SHORT;
        }
        /* 非 active 电平但 prev 也是非 active（理论不可达）：无事件 */
    }
    return ev;
}
