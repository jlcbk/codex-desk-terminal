/*
 * cdt_presenter.h — Presenter：present(state, runtime, now) → ViewModel（P2.1，A2）
 *
 * 契约：docs/INTERFACES.md §5 Presenter 行（纯转换，可在 PC 运行）、§4 优先级
 * 与计时语义。纯 C99，无 LVGL/SDL/ESP 依赖；不产生 IO。
 */
#ifndef CDT_PRESENTER_H
#define CDT_PRESENTER_H

#include "cdt_view.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 纯函数：把 AppState + DeviceRuntime 在 now_monotonic_ms（本地单调毫秒）合并为
 * ViewModel。任一输入可为 NULL：
 *   - state==NULL：尚无合法快照 → 业务字段 "--"、状态 "--"、时长冻结。
 *   - rt==NULL   ：防御处理为全默认 runtime（无电池/链路未知）。
 * view==NULL 为调用方错误，安全返回（不写）。
 *
 * 实现要点（§4）：
 *   - 电池 critical/sleep_prep → LOW_BATTERY 强制页标记（页面完整实现属 P2.3）。
 *   - 链路 stale/disconnected 提示位独立于业务状态。
 *   - source 与 link 均 fresh 才用 (now - last_rx_monotonic_ms) 推进运行/等待
 *     时长；陈旧冻结在 state 携带的 elapsed_ms/waiting_ms 基值上。
 */
void cdt_present(const cdt_app_state_t *state,
                 const cdt_runtime_t *rt,
                 uint32_t now_monotonic_ms,
                 cdt_view_t *view);

#ifdef __cplusplus
}
#endif

#endif /* CDT_PRESENTER_H */
