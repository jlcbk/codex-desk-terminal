/*
 * app_power_idle.c — Wi-Fi 空闲动态降档纯决策（ZC9 / P5.2 后半）。
 * 规则与输入/输出约定见 app_power_idle.h（宿主测试同源编译）。
 */
#include "app_power_idle.h"

app_power_idle_verdict_t app_power_idle_decide(int64_t now_ms,
                                               int64_t last_rx_ms,
                                               int64_t last_key_ms,
                                               int64_t threshold_ms,
                                               int64_t max_entered_ms)
{
    app_power_idle_verdict_t v = { APP_POWER_IDLE_PS_MIN, false };

    /* 当前档位（输入编码）：>0=正处于 MAX（值为进入时刻）；<0=不在 MAX
     * （绝对值=最近进入时刻）；0=从未进入。 */
    bool in_max = max_entered_ms > 0;
    int64_t last_entry = 0; /* 最近一次进入 MAX 的时刻；0=从未/无效 */
    if (max_entered_ms > 0) {
        last_entry = max_entered_ms;
    } else if (max_entered_ms < 0 && max_entered_ms != INT64_MIN) {
        last_entry = -max_entered_ms; /* INT64_MIN 取反回绕（UB），按无效处理 */
    }
    /* 无效防御：进入时刻来自未来 → 不可信，按「从未进入」处理（档位视为
     * MIN，迟滞门不开；空闲规则仍是主门，切换频率由阈值本身兜底）。 */
    if (last_entry > now_ms) {
        last_entry = 0;
        in_max = false;
    }

    /* 无效输入防御：时间倒流/阈值非法 → 一律 MIN（数据链路优先的保守侧）；
     * 若输入表明正处于 MAX，则要求立即拉回。 */
    if (now_ms < 0 || threshold_ms <= 0) {
        v.need_switch = in_max;
        return v;
    }

    /* 活动基准 = 两者中较新的有效值（>0 且 ≤now；未来时间戳视为无效）。 */
    int64_t activity = -1;
    if (last_rx_ms > 0 && last_rx_ms <= now_ms) {
        activity = last_rx_ms;
    }
    if (last_key_ms > activity && last_key_ms > 0 && last_key_ms <= now_ms) {
        activity = last_key_ms;
    }
    if (activity < 0) {
        /* 双双无效：无法建立空闲基准 → 不降档（保守保持 MIN）。 */
        v.need_switch = in_max;
        return v;
    }

    int64_t idle = now_ms - activity;
    bool idle_hit = idle >= threshold_ms; /* == 恰好触发（≥ 语义） */

    if (!idle_hit) {
        /* 规则②：任何新快照/按键 → 立即回 MIN（绝对规则，不受迟滞约束）。
         * 覆盖「正处于 MAX 时活动到达」与「本就 MIN」两种情形。 */
        v.need_switch = in_max;
        return v;
    }

    if (in_max) {
        /* 规则③（迟滞保持）：MAX 保持期内不做空闲侧回落评估（回落唯一
         * 途径是活动，见上）。维持现状，无需切换。 */
        v.target = APP_POWER_IDLE_PS_MAX;
        v.need_switch = false;
        return v;
    }

    /* 规则① + 再入迟滞：空闲达标才升 MAX，且「切 MAX 后至少保持 60s 才
     * 允许再评估」——最近一次进入 MAX 距今不足迟滞窗 → 阻止升档（防抖）。 */
    if (last_entry > 0 && now_ms - last_entry < APP_POWER_IDLE_HYSTERESIS_MS) {
        v.target = APP_POWER_IDLE_PS_MIN;
        v.need_switch = false;
        return v;
    }
    v.target = APP_POWER_IDLE_PS_MAX;
    v.need_switch = true;
    return v;
}
