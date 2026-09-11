/*
 * cdt_power_exec_pure.h — §7.3 停止顺序纯逻辑层（P5.3，A3 板级/功耗）
 *
 * 契约真源：
 *   docs/DEVELOPMENT_PLAN.md §7.3 停止顺序全表：
 *     阻止新更新 → 完成/放弃末帧（≤1s，失败也要睡）→ 停止 transport/无线 →
 *     关闭外设 → LCD 保持/关闭 → GPIO 安全电平/避免反向供电 → 配置唤醒 →
 *     Deep Sleep；低压原因优先 RTC 数据（不写 NVS）。
 *   shared/power/cdt_power.h（P5.1 冻结 FSM）：本层消费其动作位掩码
 *   （cdt_power_action_t）；INTERFACES §5："纯逻辑由 IDF adapter 执行"。
 *   docs/HARDWARE.md §4：深睡唤醒保底 = PWR 重新上电；KEY 深睡唤醒 unverified。
 *
 * 纯 C99：零 esp/SDL/网络/文件 IO 头（scripts/build_power_tests.sh 有门禁）。
 * 平台动作全部经注入回调表达；主机测试用内存/假时钟模拟，IDF 侧由
 * cdt_power_exec.c（同组件）接真实 esp_sleep/gpio/RTC 实现。
 */
#ifndef CDT_POWER_EXEC_PURE_H
#define CDT_POWER_EXEC_PURE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cdt_power.h" /* cdt_power_action_t（P5.1 冻结动作位掩码）*/

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* §7.3 停止顺序步骤（编号即执行序）                                     */
/* ------------------------------------------------------------------ */
typedef enum {
    CDT_PEXEC_STEP_NONE = 0,               /* 终态：流程已完成（无下一步）*/
    CDT_PEXEC_STEP_BLOCK_NEW_UPDATES = 1,  /* ① 阻止新更新：冻结新更新+取消重连 */
    CDT_PEXEC_STEP_FINAL_FRAME = 2,        /* ② 末帧：≤timeout 完成/放弃（1ms 粒度）*/
    CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO = 3, /* ③ 停 transport/无线 */
    CDT_PEXEC_STEP_POWER_OFF_PERIPH = 4,   /* ④ 关外设（音频/传感器等）*/
    CDT_PEXEC_STEP_LCD_FINAL = 5,          /* ⑤ LCD 保持或关闭（模式参数）*/
    CDT_PEXEC_STEP_GPIO_SAFE_LEVELS = 6,   /* ⑥ GPIO 安全电平（PA=46 拉低；本板无背光脚）*/
    CDT_PEXEC_STEP_CONFIG_WAKE = 7,        /* ⑦ RTC 低压原因标记 + 配置唤醒源 */
    CDT_PEXEC_STEP_ENTER_DEEP_SLEEP = 8    /* ⑧ esp_deep_sleep_start()（真机不返回）*/
} cdt_pexec_step_t;

/* 执行轨迹（观测用；IDF 执行器与主机测试共用同一记录结构）*/
#define CDT_PEXEC_TRACE_MAX 16
typedef struct {
    cdt_pexec_step_t steps[CDT_PEXEC_TRACE_MAX];
    uint8_t len;
    bool overflow; /* 超过 CDT_PEXEC_TRACE_MAX 即置位（不静默丢弃证据）*/
} cdt_pexec_trace_t;

void cdt_pexec_trace_reset(cdt_pexec_trace_t *t);
void cdt_pexec_trace_push(cdt_pexec_trace_t *t, cdt_pexec_step_t step);

/* ------------------------------------------------------------------ */
/* 顺序编排状态机：current() 给出当前应执行的步骤，执行者完成后推进。    */
/* FINAL_FRAME 步必须经 final_frame_settled() 收敛（flushed/超时/放弃   */
/* 三路都要显式记录），普通 step_done 不允许跳过它（§7.3 末帧强制的     */
/* 结构化表达）。                                                       */
/* ------------------------------------------------------------------ */
typedef struct {
    uint32_t ff_timeout_ms; /* §7.2 final_frame_timeout_ms（初值 1000）*/
    uint8_t  next_index;    /* 1..8 = 待执行步骤编号；0 未初始化；>8 终态 */
    bool     ff_flushed;    /* 末帧 flush 完成 */
    bool     ff_timed_out;  /* 末帧超时路径（§7.2：超时也睡）*/
    bool     ff_abandoned;  /* 无 flush 句柄，直接放弃（非超时）*/
    uint8_t  done_count;    /* 已完成步骤数（观测）*/
} cdt_pexec_seq_t;

void cdt_pexec_seq_init(cdt_pexec_seq_t *s, uint32_t ff_timeout_ms);
cdt_pexec_step_t cdt_pexec_seq_current(const cdt_pexec_seq_t *s);
bool cdt_pexec_seq_terminal(const cdt_pexec_seq_t *s);
/* 完成当前步骤并推进。FINAL_FRAME 步拒绝推进（返回 false），须先 settled。 */
bool cdt_pexec_seq_step_done(cdt_pexec_seq_t *s);
/* 末帧收敛（flushed/timed_out/abandoned 至少一真）并推进到步骤③。 */
void cdt_pexec_seq_final_frame_settled(cdt_pexec_seq_t *s,
                                       bool flushed, bool timed_out, bool abandoned);

/* ------------------------------------------------------------------ */
/* 末帧超时判定与等待循环（§7.2：timeout 1ms 粒度、单调时钟、同毫秒      */
/* 恰好到期即触发；与 P5.1 FSM check_timeouts 同一语义）                 */
/* ------------------------------------------------------------------ */
bool cdt_pexec_timeout_due(int64_t started_ms, int64_t now_ms, uint32_t timeout_ms);

/* 平台等待注入：poll=末帧 flush 是否完成（IDF 侧可接信号量查询）；wait=
 * 毫秒等待（1ms 粒度）；now_ms=单调毫秒。主机测试注入假时钟。          */
typedef struct {
    void    *user;
    bool   (*poll_flush)(void *user);
    void   (*wait_ms)(void *user, uint32_t ms);
    int64_t (*now_ms)(void *user);
} cdt_pexec_wait_ops_t;

#define CDT_PEXEC_WAIT_GRANULARITY_MS 1u
/* 时钟不前进时的防御性迭代上限（timeout_ms + slack），避免主机桩死循环。 */
#define CDT_PEXEC_WAIT_ITER_SLACK 16u

/* 等待末帧完成；三路结果互斥写出：
 *   flushed   = poll_flush 返回 true；
 *   timed_out = 到期仍未完成（§7.2：超时也要休眠）；
 *   abandoned = 无 flush 句柄（ops/poll_flush 为 NULL），直接放弃，不算超时。 */
void cdt_pexec_final_frame_wait(const cdt_pexec_wait_ops_t *ops, int64_t started_ms,
                                uint32_t timeout_ms,
                                bool *flushed, bool *timed_out, bool *abandoned);

/* ------------------------------------------------------------------ */
/* RTC 低压原因标记（§7.3：低压原因优先 RTC 数据；深睡后按重启处理，    */
/* 标记是 BOOT_CHECK 判"低压唤醒"的输入之一）                           */
/* ------------------------------------------------------------------ */
/* 单字节编码：bit[7:4]=0xC 魔数，bit[3]=低压原因，bit[2:0]=格式版本(=1)。
 * 32 位字低 8 位有效，高位必须为 0（脏高位按无效标记处理）。           */
#define CDT_PEXEC_REASON_MAGIC_MASK 0xF0u
#define CDT_PEXEC_REASON_MAGIC      0xC0u
#define CDT_PEXEC_REASON_LOW_BIT    0x08u
#define CDT_PEXEC_REASON_VER_MASK   0x07u
#define CDT_PEXEC_REASON_VER        0x01u

uint8_t cdt_pexec_reason_encode(bool low_battery);
bool    cdt_pexec_reason_decode(uint8_t raw, bool *low_battery_out);

/* 标记存储注入：IDF 默认实现为 RTC 慢速内存（RTC_DATA_ATTR，见
 * cdt_power_exec.c）；主机测试用内存模拟。read/write 返回 false = IO 失败。 */
typedef struct {
    void *user;
    bool (*read)(void *user, uint32_t *raw_out);
    bool (*write)(void *user, uint32_t raw);
    void (*clear)(void *user); /* 可 NULL：退化为 write(0) */
} cdt_pexec_reason_io_t;

bool cdt_pexec_reason_write(const cdt_pexec_reason_io_t *io, bool low_battery);
bool cdt_pexec_reason_read(const cdt_pexec_reason_io_t *io, bool *low_battery_out);
void cdt_pexec_reason_clear(const cdt_pexec_reason_io_t *io);

/* §7.3 BOOT_CHECK 唤醒门禁（纯判定）：低压原因标记有效 → 拒绝开无线；
 * 无标记/标记无效/读取失败 → 允许（交 P5.1 FSM 首个有效样本再判）。
 * low_wake_hint_out 可 NULL：有效低压标记时写 true（喂 cdt_power_init）。 */
bool cdt_pexec_wake_gate_allow_radio(const cdt_pexec_reason_io_t *io,
                                     bool *low_wake_hint_out);

#ifdef __cplusplus
}
#endif

#endif /* CDT_POWER_EXEC_PURE_H */
