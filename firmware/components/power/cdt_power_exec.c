/*
 * cdt_power_exec.c — §7.3 停止顺序 IDF 执行器实现（P5.3 固件侧，A3）
 *
 * 编译级验证（不烧录、不上板）：本文件是 esp 头唯一聚集点；顺序/超时/
 * 标记逻辑全部在 cdt_power_exec_pure.c（零 esp 头，主机单测覆盖），本文件
 * 只把注入回调换成平台调用点。主机测试经 tests/firmware/power_exec_stubs/
 * 的同名桩头编译本文件并计数（KEY 唤醒宏关闭时零 esp_sleep 唤醒配置调用）。
 *
 * 诚实边界：本组件不声称任何睡眠电流/LCD 保持电流/深睡保留/KEY 唤醒行为
 * 结论——全部归 P5.4 真机实测（docs/HARDWARE.md §4、§7 冲突表 #5）。
 */
#include "cdt_power_exec.h"

#include <esp_attr.h>
#include <esp_log.h>
#include <esp_sleep.h>

#include "driver/gpio.h"

#define TAG "cdt_pwr_exec"

/* ---------------- 执行器状态（单电源任务串行使用，非线程安全）-------- */

static cdt_pexec_seq_t s_seq;
static cdt_pexec_trace_t s_trace;
static cdt_power_exec_stats_t s_stats;
static volatile bool s_updates_frozen;

/* RTC 慢速内存低压原因标记（§7.3：低压原因优先 RTC 数据，不写 NVS）。
 * RTC_DATA_ATTR → .rtc.dataattr 段（RTC 慢速内存）：深睡保留；PWR 重新
 * 上电/断电冷启动后清零 → 保底恢复路径自然回到正常启动判定。 */
static RTC_DATA_ATTR uint32_t s_rtc_reason_cell;

/* ---------------- 默认 RTC 标记存储 ---------------- */

static bool rtc_reason_read(void *user, uint32_t *raw_out)
{
    (void)user;
    *raw_out = s_rtc_reason_cell;
    return true;
}

static bool rtc_reason_write(void *user, uint32_t raw)
{
    (void)user;
    s_rtc_reason_cell = raw;
    return true;
}

static void rtc_reason_clear(void *user)
{
    (void)user;
    s_rtc_reason_cell = 0u;
}

static const cdt_pexec_reason_io_t s_rtc_reason_io = {
    NULL, rtc_reason_read, rtc_reason_write, rtc_reason_clear
};

const cdt_pexec_reason_io_t *cdt_power_exec_rtc_reason_io(void)
{
    return &s_rtc_reason_io;
}

/* ---------------- 流程与观测 ---------------- */

void cdt_power_exec_flow_reset(uint32_t ff_timeout_ms)
{
    cdt_pexec_seq_init(&s_seq, ff_timeout_ms != 0u
                                   ? ff_timeout_ms
                                   : CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);
    cdt_pexec_trace_reset(&s_trace);
    s_stats.updates_frozen = false;
    s_stats.reconnect_cancelled = false;
    s_stats.final_frame_flushed = false;
    s_stats.final_frame_timed_out = false;
    s_stats.final_frame_abandoned = false;
    s_stats.reason_written = false;
    s_stats.reason_write_failed = false;
    s_stats.order_violation = false;
    s_updates_frozen = false;
}

const cdt_pexec_seq_t *cdt_power_exec_seq(void)
{
    return &s_seq;
}

const cdt_pexec_trace_t *cdt_power_exec_trace(void)
{
    return &s_trace;
}

void cdt_power_exec_stats_get(cdt_power_exec_stats_t *out)
{
    if (out != NULL) {
        *out = s_stats;
    }
}

bool cdt_power_exec_updates_frozen(void)
{
    return s_updates_frozen;
}

/* ---------------- 步骤①：阻止新更新 ---------------- */

void cdt_power_exec_prepare(cdt_power_action_t actions,
                            const cdt_power_exec_prepare_ops_t *ops)
{
    /* 只对 P5.1 FSM 的休眠路径动作生效；ALLOW_RADIO_START 等健康动作
     * 绝不误冻结新更新/误取消重连。 */
    const cdt_power_action_t sleep_path =
        CDT_POWER_ACT_ENTER_CRITICAL | CDT_POWER_ACT_CONTROLLED_SLEEP |
        CDT_POWER_ACT_BEGIN_SLEEP_PREP | CDT_POWER_ACT_BLOCK_RADIO_START;

    if ((actions & sleep_path) == CDT_POWER_ACT_NONE) {
        return;
    }
    if (cdt_pexec_seq_current(&s_seq) != CDT_PEXEC_STEP_BLOCK_NEW_UPDATES) {
        s_stats.order_violation = true; /* 未 flow_reset / 流程已推进过 */
        return;
    }

    s_updates_frozen = true; /* 冻结新更新标志（上层经 updates_frozen() 查询）*/
    s_stats.updates_frozen = true;
    if (ops != NULL && ops->cancel_reconnect != NULL) {
        ops->cancel_reconnect(ops->user); /* transport stop 注入点（P5.2 接线）*/
        s_stats.reconnect_cancelled = true;
    }
    cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_BLOCK_NEW_UPDATES);
    (void)cdt_pexec_seq_step_done(&s_seq);
}

/* ---------------- 步骤②：末帧（≤1s，失败也要睡）-------------------- */

bool cdt_power_exec_final_frame(const cdt_power_exec_wait_ops_t *ops,
                                uint32_t timeout_ms)
{
    bool flushed = false;
    bool timed_out = false;
    bool abandoned = false;
    int64_t started_ms = 0;

    if (cdt_pexec_seq_current(&s_seq) != CDT_PEXEC_STEP_FINAL_FRAME) {
        s_stats.order_violation = true;
        return false;
    }
    if (ops != NULL && ops->now_ms != NULL) {
        started_ms = ops->now_ms(ops->user);
    }

    cdt_pexec_final_frame_wait(ops, started_ms,
                               timeout_ms != 0u
                                   ? timeout_ms
                                   : CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS,
                               &flushed, &timed_out, &abandoned);

    s_stats.final_frame_flushed = flushed;
    s_stats.final_frame_timed_out = timed_out;
    s_stats.final_frame_abandoned = abandoned;
    /* 三路（完成/超时/放弃）都推进：§7.2 "失败也要休眠" */
    cdt_pexec_seq_final_frame_settled(&s_seq, flushed, timed_out, abandoned);
    cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_FINAL_FRAME);
    return flushed;
}

/* ---------------- 步骤③–⑧：停机→深睡 ---------------- */

void cdt_power_exec_sleep(const cdt_power_exec_sleep_config_t *cfg,
                          const cdt_power_exec_sleep_ops_t *ops,
                          const cdt_pexec_reason_io_t *reason_io)
{
    for (;;) {
        switch (cdt_pexec_seq_current(&s_seq)) {
        case CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO:
            if (ops != NULL && ops->stop_transport_radio != NULL) {
                ops->stop_transport_radio(ops->user);
            }
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_STOP_TRANSPORT_RADIO);
            (void)cdt_pexec_seq_step_done(&s_seq);
            break;

        case CDT_PEXEC_STEP_POWER_OFF_PERIPH:
            if (ops != NULL && ops->power_off_periph != NULL) {
                ops->power_off_periph(ops->user);
            }
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_POWER_OFF_PERIPH);
            (void)cdt_pexec_seq_step_done(&s_seq);
            break;

        case CDT_PEXEC_STEP_LCD_FINAL:
            /* 保持或关闭由模式参数决定；真实 ST7305 命令归 display 接线，
             * P5.4 做 LCD 保持/关闭电流对照后定默认（AGENTS.md：不编造效果）*/
            if (ops != NULL && ops->lcd_final != NULL && cfg != NULL) {
                ops->lcd_final(ops->user, cfg->lcd_mode);
            }
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_LCD_FINAL);
            (void)cdt_pexec_seq_step_done(&s_seq);
            break;

        case CDT_PEXEC_STEP_GPIO_SAFE_LEVELS:
            /* PA=GPIO46 高使能（HARDWARE §1.5）→ 安全电平 = 输出低，关功放。
             * 背光：本板反射 LCD 无背光引脚（HARDWARE §1 全表），无动作可做。 */
            (void)gpio_set_direction((gpio_num_t)CDT_POWER_EXEC_PA_GPIO,
                                     GPIO_MODE_OUTPUT);
            (void)gpio_set_level((gpio_num_t)CDT_POWER_EXEC_PA_GPIO, 0u);
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_GPIO_SAFE_LEVELS);
            (void)cdt_pexec_seq_step_done(&s_seq);
            break;

        case CDT_PEXEC_STEP_CONFIG_WAKE:
            /* ⑦a：低压原因写 RTC 慢速内存（BOOT_CHECK 唤醒门禁输入）。
             * 写失败不阻塞保护路径（§7.3：不能因写标记失败无限等待）。 */
            if (reason_io != NULL) {
                bool low = (cfg != NULL) && cfg->low_battery_reason;
                if (cdt_pexec_reason_write(reason_io, low)) {
                    s_stats.reason_written = true;
                } else {
                    s_stats.reason_write_failed = true;
                }
            }
            /* ⑦b：KEY 深睡唤醒（HARDWARE §4.2 unverified）。编译期宏默认 0：
             * 本分支整体编译剔除，零 esp_sleep 唤醒配置调用（链接桩断言）；
             * 开启条件 = P5.4 真机实测通过。运行期标志为第二重门禁。 */
#if CDT_CFG_KEY_DEEP_WAKE
            if (cfg != NULL && cfg->key_deep_wake) {
                (void)esp_sleep_enable_ext0_wakeup(
                    (gpio_num_t)CDT_POWER_EXEC_KEY_GPIO, 0); /* KEY 低有效 */
            }
#endif
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_CONFIG_WAKE);
            (void)cdt_pexec_seq_step_done(&s_seq);
            break;

        case CDT_PEXEC_STEP_ENTER_DEEP_SLEEP:
            cdt_pexec_trace_push(&s_trace, CDT_PEXEC_STEP_ENTER_DEEP_SLEEP);
            (void)cdt_pexec_seq_step_done(&s_seq);
            /* 先推轨迹再入睡：真机 esp_deep_sleep_start() 不返回。UART 排空
             * 等收尾归 main 集成（HARDWARE §4.3 经验），不属于本步骤顺序。 */
            ESP_LOGW(TAG, "全部 §7.3 步骤完成，进入 Deep Sleep（真机不返回）");
            esp_deep_sleep_start();
            return; /* 仅主机桩/异常环境可达 */

        case CDT_PEXEC_STEP_BLOCK_NEW_UPDATES:
        case CDT_PEXEC_STEP_FINAL_FRAME:
        case CDT_PEXEC_STEP_NONE:
        default:
            /* 顺序不符：缺 prepare/末帧或流程已终态——拒绝深睡并显式暴露，
             * 绝不静默跳过停机段入睡。 */
            s_stats.order_violation = true;
            return;
        }
    }
}

/* ---------------- BOOT_CHECK 唤醒门禁 ---------------- */

bool cdt_power_exec_boot_gate_allow_radio(const cdt_pexec_reason_io_t *io,
                                          bool *low_wake_hint_out)
{
    /* 判定逻辑在纯层（主机测试覆盖）；此处只做执行器侧接线。 */
    return cdt_pexec_wake_gate_allow_radio(io, low_wake_hint_out);
}
