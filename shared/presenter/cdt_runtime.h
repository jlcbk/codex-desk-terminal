/*
 * cdt_runtime.h — DeviceRuntime 共享类型（P1.4，A0）
 *
 * 契约：INTERFACES §4——设备本地电池/连接/页面/时钟状态；生产固件不接受
 * Bridge 覆盖电池。模拟器经独立测试接口注入（battery_sample 等经与固件相同
 * 的 Power FSM 产生保护态，P5.1；本文件只定义数据形状）。
 * 纯类型，无外部依赖；UI/Presenter 唯一输入 ViewModel 由这两者合并（P2）。
 */
#ifndef CDT_RUNTIME_H
#define CDT_RUNTIME_H

#include <stdbool.h>
#include <stdint.h>

#include "../state/codex_state.h" /* CDT_MAX_ID_BYTES */

/* §7.3 电源状态机状态（Boot→…→Deep Sleep；纯 FSM 由 shared/power/ P5.1 实现）*/
typedef enum {
    CDT_POWER_INVALID = 0,
    CDT_POWER_BOOT_CHECK = 1,
    CDT_POWER_ACTIVE = 2,
    CDT_POWER_CONNECTED_IDLE = 3,
    CDT_POWER_OFFLINE_LIGHT_SLEEP = 4,
    CDT_POWER_LOW_WARN = 5,
    CDT_POWER_CRITICAL = 6,
    CDT_POWER_SLEEP_PREP = 7,
    CDT_POWER_DEEP_SLEEP = 8
} cdt_power_state_t;

/* 链路新鲜度（INTERFACES §4：45s 未收到有效快照标 stale，150s disconnected）*/
typedef enum {
    CDT_LINK_INVALID = 0,
    CDT_LINK_CONNECTED = 1,
    CDT_LINK_STALE = 2,
    CDT_LINK_DISCONNECTED = 3
} cdt_link_state_t;

/* charging / external_power：yes / no / unknown（§4；不根据高电压猜充电）*/
typedef enum {
    CDT_PRESENCE_INVALID = 0,
    CDT_PRESENCE_YES = 1,
    CDT_PRESENCE_NO = 2,
    CDT_PRESENCE_UNKNOWN = 3
} cdt_presence_t;

/* 页面：五个业务页 + LOW_BATTERY 强制页（§6；P2.3 抢占规则；
 * DETAILS = v1.2 增补第 6 屏（ZC4 2026-09-12），普通页循环末位 */
typedef enum {
    CDT_PAGE_INVALID = 0,
    CDT_PAGE_NOW = 1,
    CDT_PAGE_AGENTS = 2,
    CDT_PAGE_PLAN = 3,
    CDT_PAGE_USAGE = 4,
    CDT_PAGE_LOW_BATTERY = 5, /* 仅由本地 Power FSM 触发，不被远端/普通切换覆盖 */
    CDT_PAGE_DETAILS = 6      /* 会话详情页（ZC4）：普通页，不参与电源仲裁 */
} cdt_page_t;

typedef struct {
    /* §7.1：battery_valid=false 时 mv/percent 无意义（对应协议 null）*/
    uint16_t battery_mv;
    bool battery_valid;
    uint8_t usable_percent; /* 0-100 单调线性估算，标注估算；非法时 0 */
    cdt_presence_t charging;
    cdt_presence_t external_power;
    cdt_power_state_t power_state;
    char transport[17]; /* "ble"/"wifi"/"mock"（§1a 枚举，<=16B）*/
    cdt_link_state_t link_state;
    uint32_t last_rx_monotonic_ms;
    cdt_page_t selected_page;
    /* 长按静音当前提醒的 attention 标识（§4 muted_attention_id，可 null）*/
    bool muted_attention_present;
    char muted_attention_id[CDT_MAX_ID_BYTES + 1];
    /* 本地时区偏移（分钟，东半球为正）：present 用 generated_at_ms+偏移渲染
     * 顶栏时钟（协议 *_at_ms 恒 UTC，仅显示换算，不参与任何状态判断）。 */
    int16_t tz_offset_min;
} cdt_runtime_t;

#endif /* CDT_RUNTIME_H */
