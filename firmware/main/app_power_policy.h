/*
 * app_power_policy.h — main.c 电源动作分发策略（单一真源，宿主测试共用）
 *
 * 审核条目：docs/ARCHITECTURE_REVIEW_2026-09-11.md R3a/R3b。
 * 抽成纯头的原因（R3a 验收要求）：主机测试用「真实 FSM + main 的策略模型」
 * 重演审核复现场景；策略判定放本头文件由 main.c 与 tests/ 同时 include，
 * 保证测试钉住的就是固件在用的掩码/样本构造，两处不漂移。
 *
 * 纯 C99：只依赖 shared/power/cdt_power.h，无平台头。
 */
#ifndef APP_POWER_POLICY_H
#define APP_POWER_POLICY_H

#include <stdbool.h>

#include "cdt_power.h"

#ifdef __cplusplus
extern "C" {
#endif

/* R3a：哪些 FSM 动作位构成「必须真实执行睡眠」的请求。
 * 旧 dispatcher（main.c 原 472–475 行）只认 ENTER_CRITICAL/BEGIN_SLEEP_PREP/
 * CONTROLLED_SLEEP，且 boot_check 把结果只用于计算允许无线的 bool——
 * DEEP_SLEEP_READY（cdt_power.c check_timeouts 末帧超时路径返回）无人处理。
 * 现启动与运行时共用本判定 + main 的 s_sleep_latched 锁存至执行。 */
static inline bool app_power_sleep_requested(cdt_power_action_t acts)
{
    return (acts & (CDT_POWER_ACT_ENTER_CRITICAL |
                    CDT_POWER_ACT_BEGIN_SLEEP_PREP |
                    CDT_POWER_ACT_CONTROLLED_SLEEP |
                    CDT_POWER_ACT_DEEP_SLEEP_READY)) != 0;
}

/* R3b：采样失败（cdt_battery_sample_batch 返回非 ESP_OK）时必须喂给 FSM 的
 * 样本形态：「当前时间、valid=false」。绝不复用上一个 valid 样本（旧代码
 * 失败分支仍送旧 s_last_sample，invalid_streak 不推进，故障保护失效且 UI
 * 反复显示旧健康电压）。Runtime 侧 battery_valid=false → presenter 电压位
 * 显示 "--"（cdt_presenter.c 电压段既有语义）。 */
static inline cdt_power_sample_t app_power_invalid_sample(int64_t now_ms)
{
    cdt_power_sample_t s;
    s.battery_mv = 0;
    s.valid = false;
    s.at_ms = now_ms;
    return s;
}

/* 裁决1（烧录验证轮落地，A0 遗留①）：§7.1 字面「连续 3 次失败进入
 * BATTERY_FAULT：关闭高功耗活动并提示」——该动作位出现时 main 必须当拍立即
 * 停 WSS（业务流/TLS 心跳即停），不等到宽限后的 §7.3 停止顺序（该顺序仍负责
 * 最终的 wss+net 全量关闭与深睡）。 */
static inline bool app_power_fault_stop_radio_now(cdt_power_action_t acts)
{
    return (acts & CDT_POWER_ACT_BATTERY_FAULT) != 0;
}

/* 裁决2（烧录验证轮落地，A0 遗留②）：ALLOW_RADIO_START 运行期接线——无线
 * 未启动（如 BOOT_CHECK 首批采样失败未过启动判定）且 FSM 发 ALLOW_RADIO_START
 * （首批恢复样本的启动判定 / recovery 稳定 10s，均见 cdt_power.c）→ 允许
 * start_radio。已启动不得因 ALLOW 重复启动；调用点须先查睡眠请求锁存
 * （睡眠优先于开无线）。 */
static inline bool app_power_runtime_radio_allow(cdt_power_action_t acts,
                                                 bool radio_started)
{
    return !radio_started && (acts & CDT_POWER_ACT_ALLOW_RADIO_START) != 0;
}

#ifdef __cplusplus
}
#endif

#endif /* APP_POWER_POLICY_H */
