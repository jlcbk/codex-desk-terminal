/*
 * cdt_key_pure.h — 按键去抖/长短按纯状态机（P4.5 固件侧，A3）
 *
 * 契约真源：docs/DEVELOPMENT_PLAN.md §6（KEY 初值：去抖 30ms，长按 800ms；
 * 在松开时判定，长按不得再触发短按）、docs/HARDWARE.md §1.2（KEY=GPIO18、
 * BOOT=GPIO0，均低有效 + 内部上拉，confirmed）。
 *
 * 纯 C99：无 ESP-IDF/SDL/网络/文件 IO/浮点；门禁扫描本文件与 .c 的 include
 * 行。输入=电平采样序列+时间戳（调用方保证 now_ms 单调不减），输出=事件；
 * 事件语义（短按轮页/长按静音）由上层映射，本层不解释业务。
 *
 * 判定语义（精确定义，测试按此断言）：
 *   - 电平去抖：raw 电平需连续保持 debounce_ms 才提交为 stable 电平；
 *     提交时间点 = raw 电平连续保持满 debounce_ms 的那次 feed 的 now_ms。
 *   - 边沿时间戳：按下/松开的"raw 边沿时间"= 该电平开始连续保持的时刻
 *     （raw_since），去抖只决定是否承认边沿，不移动边沿时刻。
 *   - 松开时判定：按下提交只记录 press_edge；松开提交时按
 *     held = 松开 raw 边沿 − 按下 raw 边沿 一次性判 LONG/SHORT：
 *       held ≥ long_press_ms → LONG（此后不再补发 SHORT）；
 *       held <  long_press_ms → SHORT。
 *   - 按住期间不产生任何事件（无重复/自动触发）；一次按压至多一个事件。
 *   - 首个样本只做采纳（上电电平），不产生事件。
 */
#ifndef CDT_KEY_PURE_H
#define CDT_KEY_PURE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* §6 初值（0 传入 init 时采用） */
#define CDT_KEY_DEBOUNCE_MS_DEFAULT 30u
#define CDT_KEY_LONG_PRESS_MS_DEFAULT 800u

/* 通用事件种类（来源 KEY/BOOT 由实例决定；完整枚举见 cdt_key.h） */
typedef enum {
    CDT_KEY_PURE_EVT_NONE = 0,
    CDT_KEY_PURE_EVT_SHORT, /* 松开时判定：< long_press_ms（且经去抖确认）*/
    CDT_KEY_PURE_EVT_LONG   /* 松开时判定：≥ long_press_ms；不再补发短按 */
} cdt_key_pure_event_t;

/* 状态机实例（公开结构：测试直接观测去抖计时） */
typedef struct {
    /* 配置（init 定） */
    uint32_t debounce_ms;   /* 电平稳定确认窗口（30）*/
    uint32_t long_press_ms; /* 长按判定阈值（800）*/
    int      active_level;  /* 按下电平（本板低有效 = 0）*/
    /* 内部状态 */
    int      raw_level;     /* 最近一次喂入电平 */
    int64_t  raw_since_ms;  /* raw_level 连续保持起点（raw 边沿时刻）*/
    int      stable_level;  /* 已去抖确认电平；-1 = 尚未采纳首样本 */
    int64_t  press_edge_ms; /* 本次按下的 raw 边沿时刻（松开判定用）*/
} cdt_key_pure_sm_t;

/* 初始化。debounce_ms/long_press_ms 传 0 取 §6 初值（30/800）。
 * active_level：本板 KEY/BOOT 均低有效 → 0。 */
void cdt_key_pure_init(cdt_key_pure_sm_t *sm, uint32_t debounce_ms,
                       uint32_t long_press_ms, int active_level);

/* 喂一个电平采样（level：0/1；now_ms：单调毫秒）。每次至多返回一个事件；
 * 无事件的采样（包括按住期间的重复电平）一律返回 NONE。 */
cdt_key_pure_event_t cdt_key_pure_feed(cdt_key_pure_sm_t *sm, int level,
                                       int64_t now_ms);

#ifdef __cplusplus
}
#endif

#endif /* CDT_KEY_PURE_H */
