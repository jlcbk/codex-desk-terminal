/*
 * app_power_idle.h — Wi-Fi 空闲动态降档决策（ZC9 / P5.2 后半；单一真源）
 *
 * 产品形态：99% 时间「连接挂着等 3.7KB 快照」（keepalive 15s）。本模块按
 * 「无数据时长」决定 Wi-Fi 省电档位（WIFI_PS_MIN_MODEM ↔ MAX_MODEM）：
 *   - 空闲达阈值 → MAX_MODEM（拉长 listen interval，等快照多花 ≤1 beacon 间隔）；
 *   - 任何新快照/按键 → 立即回 MIN_MODEM（数据链路优先，红线：不漏事件）。
 *
 * 抽成纯 C99 头+实现（零平台依赖）的原因（同 app_power_policy.h 先例）：
 * 决策规则由 main.c 与 tests/firmware/test_power_idle_pure.c 同时编译，
 * 保证宿主测试钉住的就是固件在用的判定，两处不漂移。
 *
 * esp_wifi_set_ps 的实际应用与日志在 dev_net.c（dev_net_set_idle_ps）；
 * 本模块只做纯决策，不做任何 IO。
 */
#ifndef APP_POWER_IDLE_H
#define APP_POWER_IDLE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 目标 Wi-Fi 省电档位（值刻意对齐 esp_wifi 的 wifi_ps_type_t 语义序，但不
 * 引入 esp 头；映射在 dev_net.c 完成）。 */
typedef enum {
    APP_POWER_IDLE_PS_MIN = 0, /* MIN_MODEM：默认档，射频每 DTIM 醒 */
    APP_POWER_IDLE_PS_MAX = 1  /* MAX_MODEM：每 listen_interval 醒一次 */
} app_power_idle_ps_t;

typedef struct {
    app_power_idle_ps_t target;
    bool need_switch; /* target 与输入所表达的当前档位不同 → true */
} app_power_idle_verdict_t;

/* 迟滞（防抖常量）：切到 MAX 后至少保持该时长，才允许再次评估升档。
 * 活动回落 MIN 不受此窗约束（红线：数据链路优先，立即回切）。
 * 注：Kconfig 阈值下限 1 分钟 ≥ 本常量，故「活动→MIN→再升 MAX」的自然间隔
 * ≥ 阈值 ≥ 60s；本常量在阈值被配得小于 60s（宿主测试域/未来配置）时兜底
 * 限制 MAX/MIN 翻动频率 ≥ 一次/60s。 */
#define APP_POWER_IDLE_HYSTERESIS_MS ((int64_t)60000)

/*
 * 输入约定（全部为同一单调毫秒源；0/负值 = 「从未/无效」）：
 *   now_ms          当前单调毫秒；<0 视为无效输入；
 *   last_rx_ms      最近一次「合法新 seq 快照已应用」时刻；≤0 = 从未/无效；
 *   last_key_ms     最近一次按键（短按或长按）时刻；≤0 = 从未/无效；
 *   threshold_ms    降档阈值；≤0 视为无效输入；
 *   max_entered_ms  档位状态编码：
 *                     >0：当前正处于 MAX，值为本次进入时刻；
 *                     <0：当前不在 MAX，绝对值 = 最近一次进入 MAX 的时刻；
 *                      0：从未进入过 MAX。
 *
 * 防御规则：无法建立空闲基准（双活动时间戳无效）、未来时间戳（>now）、
 * now<0、threshold≤0 一律 target=MIN（数据链路优先的保守侧）。
 * 阈值边界：now - activity == threshold 恰好触发（≥ 语义）。
 */
app_power_idle_verdict_t app_power_idle_decide(int64_t now_ms,
                                               int64_t last_rx_ms,
                                               int64_t last_key_ms,
                                               int64_t threshold_ms,
                                               int64_t max_entered_ms);

#ifdef __cplusplus
}
#endif

#endif /* APP_POWER_IDLE_H */
