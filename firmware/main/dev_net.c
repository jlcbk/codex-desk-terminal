/*
 * dev_net.c — WiFi STA 接线实现（整机集成 v1；契约见 dev_net.h）。
 *
 * 退避序列与 ±20% 抖动复述 protocol/transport.md §5.4（与 cdt_wss_client /
 * bridge link.py 同源冻结值）；断开计数事件来自 WIFI_EVENT_STA_DISCONNECTED，
 * 重连动作由 dev_net_poll 在到点时发起（事件回调内不阻塞）。
 */
#include "dev_net.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_netif.h"

#define TAG "dev_net"

/* 冻结退避（§5.4）：1/2/4/8/16/30s 封顶，±20% 均匀抖动 */
static const uint32_t s_backoff_ms[] = { 1000, 2000, 4000, 8000, 16000, 30000 };
#define BACKOFF_LEN (sizeof s_backoff_ms / sizeof s_backoff_ms[0])

static dev_net_state_t s_state = DEV_NET_STOPPED;
static bool s_started;
static uint32_t s_attempts;
static int64_t s_next_retry_ms;
static char s_ip_str[16];
static int s_retry_count; /* 观测：累计断开重连次数 */

const char *dev_net_state_name(dev_net_state_t st)
{
    switch (st) {
    case DEV_NET_STOPPED:    return "stopped";
    case DEV_NET_CONNECTING: return "connecting";
    case DEV_NET_CONNECTED:  return "connected";
    case DEV_NET_BACKOFF:    return "backoff";
    default:                 return "?";
    }
}

static void set_state(dev_net_state_t st)
{
    if (s_state != st) {
        s_state = st;
        ESP_LOGI(TAG, "state -> %s", dev_net_state_name(st));
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "STA start -> connect");
        set_state(DEV_NET_CONNECTING);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_retry_count++;
        uint32_t idx = s_attempts;
        if (idx >= BACKOFF_LEN) {
            idx = BACKOFF_LEN - 1;
        }
        s_attempts++;
        /* ±20% 均匀抖动：r ∈ [-200, 200] 千分比 */
        int32_t r = (int32_t)(esp_random() % 401u) - 200;
        uint32_t delay = (uint32_t)(((int64_t)s_backoff_ms[idx] * (1000 + r)) / 1000);
        s_next_retry_ms = esp_timer_get_time() / 1000LL + (int64_t)delay;
        ESP_LOGW(TAG, "断开（第 %d 次），%lu ms 后重试", s_retry_count, (unsigned long)delay);
        set_state(DEV_NET_BACKOFF);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        snprintf(s_ip_str, sizeof s_ip_str, IPSTR, IP2STR(&ev->ip_info.ip));
        s_attempts = 0; /* 稳定即重置退避档 */
        ESP_LOGI(TAG, "取到 IP %s", s_ip_str);
        set_state(DEV_NET_CONNECTED);
    }
}

esp_err_t dev_net_start(const char *ssid, const char *pass)
{
    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (ssid == NULL || ssid[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    wifi_config_t wc;
    memset(&wc, 0, sizeof wc);
    strncpy((char *)wc.sta.ssid, ssid, sizeof wc.sta.ssid - 1);
    strncpy((char *)wc.sta.password, pass ? pass : "", sizeof wc.sta.password - 1);
    wc.sta.threshold.authmode = WIFI_AUTH_WPA_PSK; /* 开放/WEP 拒绝 */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_LOGI(TAG, "连接 SSID=\"%s\"（密码不落日志）", ssid);
    ESP_ERROR_CHECK(esp_wifi_start());
    s_started = true;
    s_ip_str[0] = '\0';
    return ESP_OK;
}

esp_err_t dev_net_stop(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_wifi_disconnect();
    esp_wifi_stop();
    s_started = false;
    set_state(DEV_NET_STOPPED);
    ESP_LOGW(TAG, "STA 已停用（LOW BATTERY 无线关闭）");
    return ESP_OK;
}

void dev_net_poll(int64_t now_ms)
{
    if (s_started && s_state == DEV_NET_BACKOFF && now_ms >= s_next_retry_ms) {
        ESP_LOGI(TAG, "退避到点 -> 重连");
        set_state(DEV_NET_CONNECTING);
        esp_wifi_connect();
    }
}

dev_net_state_t dev_net_state(void)
{
    return s_state;
}

bool dev_net_ip_str(char *out, size_t cap)
{
    if (out == NULL || cap == 0) {
        return false;
    }
    out[0] = '\0';
    if (s_state != DEV_NET_CONNECTED || s_ip_str[0] == '\0') {
        return false;
    }
    snprintf(out, cap, "%s", s_ip_str);
    return true;
}
