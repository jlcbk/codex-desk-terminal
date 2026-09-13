/*
 * dev_net.h — WiFi STA 接线（整机集成 v1，A3+A4 合并；firmware/main 内聚）。
 *
 * 契约真源：docs/INTERFACES.md §6（Wi-Fi 适配）、protocol/transport.md §5.4
 * （无线断连退避 1/2/4/8/16/30s ±20% 抖动）。凭据经 dev_net_config.h 注入
 * （main.c 负责 include 与传递），本模块不持有任何凭据常量。
 *
 * 边界（AGENTS.md）：本模块只管 STA 链路（连接/断开/退避/IP），不解释任何
 * 业务状态；WSS 重连归 components/transport（cdt_wss_client 冻结退避）。
 * 省电策略（modem-sleep/自动 Light Sleep）归 P5.2，本模块用 IDF 默认值。
 */
#ifndef CDT_DEV_NET_H
#define CDT_DEV_NET_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    DEV_NET_STOPPED = 0,   /* 尚未 start（BOOT_CHECK 拒绝无线时停留在此） */
    DEV_NET_CONNECTING = 1,
    DEV_NET_CONNECTED = 2, /* 已取 IP */
    DEV_NET_BACKOFF = 3    /* 断开，退避等待重试（§5.4 序列） */
} dev_net_state_t;

const char *dev_net_state_name(dev_net_state_t st);

/* 初始化 netif/事件环/wifi STA 并启动连接（幂等保护：已启动返回 ESP_ERR_INVALID_STATE）。
 * 失败重试退避在本模块内部按冻结序列驱动（poll 驱动，见 dev_net_poll）。 */
esp_err_t dev_net_start(const char *ssid, const char *pass);

/* 主动停用（LOW BATTERY 无线关闭路径；CRITICAL 后不再重连）。 */
esp_err_t dev_net_stop(void);

/* ZC9（P5.2 后半）空闲动态降档：应用 Wi-Fi 省电档。
 * idle_max=true → WIFI_PS_MAX_MODEM（空闲降档），false → WIFI_PS_MIN_MODEM
 * （默认/活动档）。reason 为触发原因字符串（进 INFO 日志，可 NULL）。
 * 档位相对当前已应用值无变化时不调驱动不打日志（限频）。无线未启动返回
 * ESP_ERR_INVALID_STATE。决策逻辑单源 app_power_idle.c（main 周期调用）。 */
esp_err_t dev_net_set_idle_ps(bool idle_max, const char *reason);

/* 主循环周期调用：驱动断开重连退避节奏（非阻塞；到点才 esp_wifi_connect）。 */
void dev_net_poll(int64_t now_ms);

dev_net_state_t dev_net_state(void);
/* 已取 IP 时写 out（长度≥16）；返回是否已连接。 */
bool dev_net_ip_str(char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* CDT_DEV_NET_H */
