/*
 * cdt_power.h — 共享纯 Power FSM（P5.1，A3 板级/功耗）
 *
 * 契约真源：
 *   docs/DEVELOPMENT_PLAN.md §7.1 采样/有效范围/故障、§7.2 参数与"连续"定义、
 *   §7.3 状态转移表；docs/INTERFACES.md §4（battery_sample 经与固件相同的 FSM
 *   产生 LOW BATTERY，本文件即该唯一实现，模拟器与固件共用）与 §5（Power FSM：
 *   step(sample,event,now)→actions，纯逻辑；关无线/睡眠等动作由平台 adapter 执行）。
 *   docs/HARDWARE.md §4：深睡唤醒保底 = PWR 重新上电（冷启动重新 cdt_power_init），
 *   KEY 深睡唤醒未实证，本 FSM 不建模深睡后的任何唤醒输入。
 *
 * 纯 C99：无 ESP-IDF/SDL/网络/文件 IO，无浮点；时间一律单调毫秒（int64），
 * 电压一律毫伏整数比较。状态枚举复用 shared/presenter/cdt_runtime.h 的
 * cdt_power_state_t（只读复用，本文件不重复定义）。
 *
 * 调用约定：调用方保证输入时间单调（sample.at_ms ≤ 同次 step 的 now_ms）。
 * 本任务（P5.1）参数全部取 §7.2 初值且可注入；P4.4 真实校准只回填参数，
 * 不改 FSM 结构。
 */
#ifndef CDT_POWER_H
#define CDT_POWER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../presenter/cdt_runtime.h" /* cdt_power_state_t 唯一定义处，勿在此重复 */

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* §7.2 初始参数（集中配置，可校准）+ §7.1 有效范围 + 校准注入          */
/* ------------------------------------------------------------------ */
typedef struct {
    /* §7.2 表初值 */
    uint16_t low_enter_mv;           /* 3700：LOW_WARN 进入（≤）*/
    uint16_t low_exit_mv;            /* 3750：警告解除（严格大于，连续稳定 10s）*/
    uint16_t critical_mv;            /* 3600：≤ 此值计持续低压时间 */
    uint16_t recovery_mv;            /* 3700：低压唤醒/深睡后恢复门槛（≥，稳定 10s）*/
    uint32_t critical_hold_ms;       /* 30000：连续有效低压 30s 后必须准备睡眠 */
    uint32_t stable_ms;              /* 10000：low_exit / recovery 共用的"稳定 10s" */
    uint32_t final_frame_timeout_ms; /* 1000：末帧最多等 1s，失败也要休眠 */
    /* §7.2 连续定义 / §7.1 故障策略 */
    uint32_t sample_gap_max_ms;      /* 2000：连续有效样本间隔上限；超限转采样故障检查 */
    uint8_t  fault_fail_count;       /* 3：连续 3 次无效样本 → BATTERY_FAULT */
    uint32_t fault_grace_ms;         /* 10000：故障 10s 不可恢复 → 受控休眠 */
    /* §7.1 电压有效范围初值 2500–4500mV；范围外按 unknown/失败样本计 */
    uint16_t valid_min_mv;           /* 2500 */
    uint16_t valid_max_mv;           /* 4500 */
    /* §7.1 校准注入（P4.4 预留）：
     * effective_mv = raw_mv × cal_gain_ppm / 1000000 + cal_offset_mv。
     * 增益用整数 ppm 表示，默认恒等变换；仅作用于阈值判定，不改写输入样本。 */
    int32_t  cal_offset_mv;          /* 默认 0 */
    int32_t  cal_gain_ppm;           /* 默认 1000000（= ×1.0）*/
} cdt_power_params_t;

/* ------------------------------------------------------------------ */
/* 输入：电压样本与事件（INTERFACES §4/§5）                             */
/* ------------------------------------------------------------------ */
typedef struct {
    uint16_t battery_mv; /* 已按 HAL 校准链（§7.1 公式）还原的电池端电压 */
    bool     valid;      /* false = 采样失败/unknown（§7.1：范围外/驱动失败在
                              HAL 侧标 invalid；本 FSM 另对范围做兜底检查）*/
    int64_t  at_ms;      /* 本样本单调时间戳（毫秒）*/
} cdt_power_sample_t;

typedef enum {
    CDT_POWER_EVT_NONE = 0,
    CDT_POWER_EVT_ACTIVITY,         /* 活动/新状态到达/待 flush（§7.3 ACTIVE 行）*/
    CDT_POWER_EVT_IDLE_REACHED,     /* 无脏画面/无任务 → CONNECTED_IDLE 占位（P5.2 定策略）*/
    CDT_POWER_EVT_OFFLINE_SLEEP,    /* 主动离线节能 → OFFLINE_LIGHT_SLEEP 占位（P5.2 定策略）*/
    CDT_POWER_EVT_FINAL_FRAME_DONE, /* 末帧 flush 完成（成功或放弃）（§7.3 SLEEP_PREP 行）*/
    CDT_POWER_EVT_KEY_WAKE          /* KEY 唤醒/本地唤醒事件（§7.3 OFFLINE_LIGHT_SLEEP 行）*/
} cdt_power_event_t;

typedef enum {
    CDT_POWER_IN_NONE = 0, /* 纯时钟推进：只做超时/保持检查（§7.2 全部用单调时钟判定）*/
    CDT_POWER_IN_SAMPLE,   /* 使用 sample 字段 */
    CDT_POWER_IN_EVENT     /* 使用 event 字段 */
} cdt_power_input_kind_t;

typedef struct {
    cdt_power_input_kind_t kind;
    cdt_power_sample_t sample; /* kind == CDT_POWER_IN_SAMPLE 时有效 */
    cdt_power_event_t event;   /* kind == CDT_POWER_IN_EVENT 时有效 */
} cdt_power_input_t;

/* ------------------------------------------------------------------ */
/* 动作：位掩码即一次 step 返回的"动作列表"，由平台 adapter 执行        */
/* （INTERFACES §5：纯逻辑由 IDF adapter 执行关无线/睡眠等动作）        */
/* ------------------------------------------------------------------ */
typedef uint32_t cdt_power_action_t;

#define CDT_POWER_ACT_NONE                      ((cdt_power_action_t)0)
/* §7.3 LOW_WARN 进入：电压警告、快速采样、业务继续 */
#define CDT_POWER_ACT_ENTER_LOW_WARN            ((cdt_power_action_t)1u << 0)
/* §7.2 警告解除：>3750mV 连续稳定 10s */
#define CDT_POWER_ACT_EXIT_LOW_WARN             ((cdt_power_action_t)1u << 1)
/* §7.3 CRITICAL 进入：抢占普通任务、取消重连、锁存（含电池故障安全路径）*/
#define CDT_POWER_ACT_ENTER_CRITICAL            ((cdt_power_action_t)1u << 2)
/* §7.3 SLEEP_PREP 进入：LOW BATTERY/故障页、冻结末帧等待 ≤1s */
#define CDT_POWER_ACT_BEGIN_SLEEP_PREP          ((cdt_power_action_t)1u << 3)
/* §7.3 DEEP_SLEEP：末帧完成或超时，现在可入深睡（保留原因由 adapter 落 RTC）*/
#define CDT_POWER_ACT_DEEP_SLEEP_READY          ((cdt_power_action_t)1u << 4)
/* §7.2 final_frame_timeout_ms：末帧超时（与 DEEP_SLEEP_READY 同现）*/
#define CDT_POWER_ACT_FINAL_FRAME_TIMEOUT       ((cdt_power_action_t)1u << 5)
/* §7.1 连续 3 次失败进入 BATTERY_FAULT：关闭高功耗活动并提示 */
#define CDT_POWER_ACT_BATTERY_FAULT             ((cdt_power_action_t)1u << 6)
/* §7.1 故障成立后 10s 内出现有效样本：恢复 */
#define CDT_POWER_ACT_BATTERY_FAULT_RECOVERED   ((cdt_power_action_t)1u << 7)
/* §7.1 故障 10s 仍不可恢复 → 受控休眠（无已验证外部供电时；经 CRITICAL 路径）*/
#define CDT_POWER_ACT_CONTROLLED_SLEEP          ((cdt_power_action_t)1u << 8)
/* §7.3 BOOT_CHECK：低压唤醒拒绝启动无线（先 ADC 后无线；recovery 未满足）*/
#define CDT_POWER_ACT_BLOCK_RADIO_START         ((cdt_power_action_t)1u << 9)
/* 健康启动或 recovery（≥recovery_mv 稳定 10s）通过：允许启动无线 */
#define CDT_POWER_ACT_ALLOW_RADIO_START         ((cdt_power_action_t)1u << 10)
/* §7.2 连续有效样本间隔 >2s：持续低压计时重置、转采样故障检查（缺测不计入）*/
#define CDT_POWER_ACT_SAMPLE_GAP_RESET          ((cdt_power_action_t)1u << 11)
/* 占位动作（§7.3 三态进入条件本任务只落状态；无线策略实测归 P5.2）*/
#define CDT_POWER_ACT_ENTER_ACTIVE              ((cdt_power_action_t)1u << 12)
#define CDT_POWER_ACT_ENTER_CONNECTED_IDLE      ((cdt_power_action_t)1u << 13)
#define CDT_POWER_ACT_ENTER_OFFLINE_LIGHT_SLEEP ((cdt_power_action_t)1u << 14)

/* ------------------------------------------------------------------ */
/* FSM 实例（公开结构：模拟器/测试可直接观测内部计时）                  */
/* ------------------------------------------------------------------ */
typedef struct {
    cdt_power_params_t params;
    cdt_power_state_t  state;        /* 复用 cdt_runtime.h 枚举 */

    bool    wake_low_hint;           /* init 注入：RTC 记录的低压唤醒原因（§7.3）*/
    bool    boot_decided;            /* 已收到首个有效样本并完成启动判定 */
    bool    boot_recovery_required;  /* 低压唤醒：要求 ≥recovery_mv 稳定才 ACTIVE */

    bool    critical_latched;        /* §7.2：CRITICAL 成立后锁存，不因反弹撤销 */
    bool    crit_running;            /* 当前连续 ≤critical_mv 段正在计时 */
    int64_t crit_start_ms;           /* 本段首个 ≤critical_mv 有效样本时间 */

    bool    lowexit_running;         /* LOW_WARN 退出稳定计时（>low_exit_mv 连续）*/
    int64_t lowexit_start_ms;

    bool    recov_running;           /* recovery 稳定计时（≥recovery_mv 连续）*/
    int64_t recov_start_ms;

    bool    have_last_valid;         /* §7.2 连续样本间隔检查 */
    int64_t last_valid_at_ms;
    uint32_t gap_reset_count;        /* 缺测/乱序导致计时重置的次数（观测）*/

    uint8_t invalid_streak;          /* 连续无效样本计数 */
    bool    battery_fault;           /* BATTERY_FAULT 成立（无独立状态枚举，见报告）*/
    int64_t fault_since_ms;

    bool    final_frame_pending;     /* 已请求冻结末帧，等待 flush 完成 */
    bool    final_frame_done;        /* flush 完成事件已到达 */
    bool    final_frame_timed_out;   /* 走的 1s 超时路径（观测）*/
    int64_t sleep_prep_since_ms;     /* SLEEP_PREP 进入时间 */
} cdt_power_fsm_t;

/* ------------------------------------------------------------------ */
/* API                                                                  */
/* ------------------------------------------------------------------ */

/* 填入 §7.1/§7.2 全部初值（含校准恒等）。 */
void cdt_power_params_init(cdt_power_params_t *params);

/* 初始化 FSM：状态 = CDT_POWER_BOOT_CHECK（§7.3 开机/深睡唤醒必经）。
 * wake_low_hint = RTC 保留的低压唤醒原因；false 时由首个有效样本决定
 * （首次样本即 ≤critical_mv 亦视为低压唤醒）。深睡后恢复走 PWR 重新上电
 * （HARDWARE §4 保底），即重新调用本函数冷启动。 */
void cdt_power_init(cdt_power_fsm_t *fsm, const cdt_power_params_t *params,
                    bool wake_low_hint);

/* 纯函数式步进：注入一个样本或事件 + 当前单调时间（毫秒）。in 为 NULL 等价于
 * 纯时钟推进。返回本次调用产生的动作列表（位掩码，可多个同现）。
 * 输入处理先行，超时/保持检查随后（同一 now_ms 下"恰好到期"即触发）。 */
cdt_power_action_t cdt_power_step(cdt_power_fsm_t *fsm,
                                  const cdt_power_input_t *in, int64_t now_ms);

/* §7.1 校准注入换算（P4.4 用）：effective = raw × gain_ppm / 1e6 + offset。 */
int32_t cdt_power_effective_mv(const cdt_power_params_t *params, uint16_t raw_mv);

/* 诊断辅助（纯函数，无 IO）：状态名 / 动作位名（写入调用方缓冲，'|' 分隔）。 */
const char *cdt_power_state_str(cdt_power_state_t state);
const char *cdt_power_actions_str(char *buf, size_t cap, cdt_power_action_t actions);

#ifdef __cplusplus
}
#endif

#endif /* CDT_POWER_H */
