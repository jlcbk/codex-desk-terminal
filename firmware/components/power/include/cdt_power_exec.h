/*
 * cdt_power_exec.h — §7.3 停止顺序 IDF 执行器（P5.3 固件侧，A3；编译级验证）
 *
 * 契约真源：
 *   docs/DEVELOPMENT_PLAN.md §7.3 停止顺序全表 + §7.2 final_frame_timeout_ms；
 *   INTERFACES §5：纯逻辑（shared/power P5.1 FSM）返回动作位掩码，关无线/
 *   睡眠等动作由本执行器执行；docs/HARDWARE.md §1.2（KEY=GPIO18 低有效）、
 *   §1.5（PA=GPIO46 高使能）、§4（深睡唤醒保底 = PWR 重新上电；KEY 深睡
 *   唤醒 unverified，§7 冲突表 #5）。
 *
 * 边界（AGENTS.md）：
 *   - 不直接依赖 transport/display 组件：平台动作全部经函数指针注入；
 *   - 本头文件零 esp 头（esp 头只出现在 cdt_power_exec.c）；
 *   - 编译级验证：不声称任何睡眠电流/LCD 保持电流/唤醒行为结论，实测归 P5.4。
 */
#ifndef CDT_POWER_EXEC_H
#define CDT_POWER_EXEC_H

#include <stdbool.h>
#include <stdint.h>

#include "cdt_power.h"             /* cdt_power_action_t（P5.1 冻结）*/
#include "cdt_power_exec_pure.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 配置宏与板级常量（引脚取 docs/HARDWARE.md 引脚表）                   */
/* ------------------------------------------------------------------ */

/* KEY 深睡唤醒编译开关。默认 0（关闭）：
 *   docs/HARDWARE.md §4.2 —— KEY(GPIO18) 做深睡唤醒源 = unverified，本板
 *   无任何深睡唤醒实测/代码先例；开启条件 = P5.4 真机实测通过。关闭期间
 *   执行器零 esp_sleep 唤醒配置调用（tests/firmware/test_power_exec_pure.c
 *   链接桩计数断言），深睡后唯一保底恢复路径 = PWR 重新上电（冷启动，
 *   §7.3 DEEP_SLEEP 行）。不得在未实测的 GPIO 上配置"理论唤醒"。 */
#ifndef CDT_CFG_KEY_DEEP_WAKE
#define CDT_CFG_KEY_DEEP_WAKE 0
#endif

/* 功放使能 PA：GPIO46，高使能（HARDWARE §1.5，XiaoZhi config.h:20 + wiki）。
 * GPIO 安全电平 = 拉低（关功放，避免深睡时扬声器回路意外供电）。 */
#define CDT_POWER_EXEC_PA_GPIO 46

/* KEY 键：GPIO18，低有效，内部上拉（HARDWARE §1.2 官方 button_bsp.c:17-19）。
 * 仅 CDT_CFG_KEY_DEEP_WAKE=1 且运行期标志同真时用于 ext0 唤醒配置。 */
#define CDT_POWER_EXEC_KEY_GPIO 18

/* 背光：本板为 4.2" 反射 LCD，HARDWARE §1 全表（vendor/实战/wiki 三路）
 * 无背光引脚——GPIO 安全电平步骤没有背光电平动作可做（"背光无"）。 */
/* #define CDT_POWER_EXEC_BACKLIGHT_GPIO 不存在：见上行说明 */

/* §7.2 初值：末帧最多等待 1 秒，失败也要休眠。 */
#define CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS 1000u

/* ------------------------------------------------------------------ */
/* 类型                                                                 */
/* ------------------------------------------------------------------ */

/* LCD 末态两模式（§7.3 步⑤；保持/关闭对照与电流实测归 P5.4）*/
typedef enum {
    CDT_POWER_EXEC_LCD_HOLD = 0, /* 保持：ST7305 自刷新保持 LOW BATTERY 画面 */
    CDT_POWER_EXEC_LCD_OFF = 1   /* 关闭：保持功耗实测超标时的电池保护路径 */
} cdt_power_exec_lcd_mode_t;

/* 步骤①注入：取消重连（transport stop 经函数指针注入，不依赖 P3 骨架组件）*/
typedef struct {
    void *user;
    void (*cancel_reconnect)(void *user);
} cdt_power_exec_prepare_ops_t;

/* 末帧等待注入（== cdt_pexec_wait_ops_t；IDF 侧 poll_flush 可接 flush 完成
 * 信号量查询，wait_ms 接 vTaskDelay，now_ms 接 esp_timer）*/
typedef cdt_pexec_wait_ops_t cdt_power_exec_wait_ops_t;

/* 步骤③④⑤注入（真实 ST7305 末态命令、无线停机接线归 P4.3/P5.2 集成）*/
typedef struct {
    void *user;
    void (*stop_transport_radio)(void *user);
    void (*power_off_periph)(void *user);
    void (*lcd_final)(void *user, cdt_power_exec_lcd_mode_t mode);
} cdt_power_exec_sleep_ops_t;

/* 深睡入口参数（§7.3 步⑤⑦ + ⑧）*/
typedef struct {
    cdt_power_exec_lcd_mode_t lcd_mode;
    bool key_deep_wake;      /* 还须 CDT_CFG_KEY_DEEP_WAKE=1 才生效（双重门禁）*/
    bool low_battery_reason; /* RTC 原因标记内容（BOOT_CHECK 唤醒门禁输入）*/
} cdt_power_exec_sleep_config_t;

/* 执行统计（观测；与 cdt_pexec_trace_t 一起构成验收证据）*/
typedef struct {
    bool updates_frozen;        /* 步骤①：已冻结新更新 */
    bool reconnect_cancelled;   /* 步骤①：已注入取消重连 */
    bool final_frame_flushed;   /* 步骤②：flush 完成 */
    bool final_frame_timed_out; /* 步骤②：超时路径（仍要睡，§7.2）*/
    bool final_frame_abandoned; /* 步骤②：无句柄放弃（非超时）*/
    bool reason_written;        /* 步骤⑦：RTC 原因标记写入成功 */
    bool reason_write_failed;   /* 步骤⑦：写入失败（仍继续睡，不因标记失败阻塞保护）*/
    bool order_violation;       /* 调用顺序不符 §7.3（拒绝执行并显式暴露）*/
} cdt_power_exec_stats_t;

/* ------------------------------------------------------------------ */
/* API                                                                  */
/* ------------------------------------------------------------------ */

/* 开始一次 CRITICAL→深睡流程（重置顺序/轨迹/统计/冻结标志）。
 * ff_timeout_ms 传 0 时取 CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS。 */
void cdt_power_exec_flow_reset(uint32_t ff_timeout_ms);

const cdt_pexec_seq_t *cdt_power_exec_seq(void);
const cdt_pexec_trace_t *cdt_power_exec_trace(void);
void cdt_power_exec_stats_get(cdt_power_exec_stats_t *out);
bool cdt_power_exec_updates_frozen(void);

/* 步骤①：actions 含 ENTER_CRITICAL / CONTROLLED_SLEEP / BEGIN_SLEEP_PREP /
 * BLOCK_RADIO_START 任一（P5.1 FSM 的休眠路径动作）才执行；健康启动动作
 * （ALLOW_RADIO_START 等）不会误冻结。 */
void cdt_power_exec_prepare(cdt_power_action_t actions,
                            const cdt_power_exec_prepare_ops_t *ops);

/* 步骤②：等待末帧 ≤ timeout_ms（0 → 用默认 1000ms）。返回 flushed。
 * 超时/放弃均推进顺序（§7.2：失败也要休眠）。 */
bool cdt_power_exec_final_frame(const cdt_power_exec_wait_ops_t *ops,
                                uint32_t timeout_ms);

/* 步骤③–⑧：停 transport/无线 → 关外设 → LCD 保持/关闭 → GPIO 安全电平
 * （PA=46 拉低；本板无背光脚）→ RTC 原因标记 + 配唤醒源 →
 * esp_deep_sleep_start()（真机不返回）。顺序不符时拒绝执行并置
 * order_violation（缺末帧/缺 prepare 时绝不静默深睡）。 */
void cdt_power_exec_sleep(const cdt_power_exec_sleep_config_t *cfg,
                          const cdt_power_exec_sleep_ops_t *ops,
                          const cdt_pexec_reason_io_t *reason_io);

/* §7.3 BOOT_CHECK 唤醒门禁：读 RTC 保留标记，返回是否允许开无线。
 * 低压原因标记有效 → false（拒无线）且 *low_wake_hint_out=true（可喂
 * cdt_power_init 的 wake_low_hint，要求 recovery 后才恢复）。 */
bool cdt_power_exec_boot_gate_allow_radio(const cdt_pexec_reason_io_t *io,
                                          bool *low_wake_hint_out);

/* 默认标记存储：RTC 慢速内存静态字（RTC_DATA_ATTR）。深睡保留；PWR 重新
 * 上电/断电冷启动清零（即保底路径下自然回到正常启动判定）。保留行为
 * 本身属 P5.4 实测项，本组件不声称深睡保留已验证。 */
const cdt_pexec_reason_io_t *cdt_power_exec_rtc_reason_io(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_POWER_EXEC_H */
