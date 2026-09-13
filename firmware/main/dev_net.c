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

/* P5.2 前半：PS 模式名（仅日志用） */
static const char *ps_mode_name(wifi_ps_type_t ps)
{
    switch (ps) {
    case WIFI_PS_NONE:     return "NONE";
    case WIFI_PS_MIN_MODEM: return "MIN_MODEM";
    case WIFI_PS_MAX_MODEM: return "MAX_MODEM";
    default:               return "INVALID";
    }
}

/* ZC9（P5.2 后半）：当前已应用的省电档（-1 = 尚未知，如 start 中 set_ps
 * 失败）；dev_net_set_idle_ps 据此去重，保证「只在档位真变化时调驱动/打
 * 日志」（限频不刷屏）。 */
static int s_ps_applied = -1;

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

    /* P5.2 前半（连接稳定性）：省电模式显式化——IDF 默认 MIN_MODEM 不再隐式。
     * A/B 结论见 artifacts/board/p52/report.md；CONNECTED_IDLE 的 modem-sleep
     * 精细联动（PM 锁/显示联动）归 P5.2 后半，功耗实测归 P6.1 仪器。 */
    wifi_ps_type_t ps = WIFI_PS_MIN_MODEM;
#if CONFIG_CDT_WIFI_PS_MODE == 0
    ps = WIFI_PS_NONE;
#elif CONFIG_CDT_WIFI_PS_MODE == 2
    ps = WIFI_PS_MAX_MODEM;
#endif
    esp_err_t ps_err = esp_wifi_set_ps(ps);
    if (ps_err == ESP_OK) {
        s_ps_applied = (int)ps;
    }
    wifi_ps_type_t ps_now = WIFI_PS_NONE; /* get 回读失败时日志显示 rc；-1 兜底不可行，用 NONE 占位 */
    esp_err_t get_err = esp_wifi_get_ps(&ps_now);
    ESP_LOGI(TAG, "WiFi PS 模式 set=%s(rc=%s) get=%s(rc=%s)（睡眠时长未测，归 P6.1）",
             ps_mode_name(ps), esp_err_to_name(ps_err),
             ps_mode_name(ps_now), esp_err_to_name(get_err));

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
    s_ps_applied = -1; /* STA 停用后档位未知；下次 start 重新应用并记录 */
    set_state(DEV_NET_STOPPED);
    ESP_LOGW(TAG, "STA 已停用（LOW BATTERY 无线关闭）");
    return ESP_OK;
}

/* ZC9（P5.2 后半）：空闲动态降档的档位应用入口。idle_max=true → MAX_MODEM
 * （拉长 listen interval），false → MIN_MODEM（默认档）。只在档位相对当前
 * 已应用值真变化时才调 esp_wifi_set_ps 并打一条 INFO（含档位与触发原因；
 * 调用方 main 的决策已按 need_switch 限频，此处再按档位去重兜底，不刷屏）。
 * 数据链路优先红线：本函数不做任何业务判断，何时切由 app_power_idle 决策。 */
esp_err_t dev_net_set_idle_ps(bool idle_max, const char *reason)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    wifi_ps_type_t want = idle_max ? WIFI_PS_MAX_MODEM : WIFI_PS_MIN_MODEM;
    if ((int)want == s_ps_applied) {
        return ESP_OK; /* 已在该档：不重复 set/log */
    }
    esp_err_t err = esp_wifi_set_ps(want);
    if (err == ESP_OK) {
        s_ps_applied = (int)want;
        ESP_LOGI(TAG, "PS -> %s（%s）", ps_mode_name(want),
                 reason != NULL ? reason : "-");
    } else {
        ESP_LOGW(TAG, "PS -> %s 失败: %s", ps_mode_name(want),
                 esp_err_to_name(err));
    }
    return err;
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
