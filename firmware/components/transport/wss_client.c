/*
 * wss_client.c — WSS 设备客户端骨架实现（P3.3/P3.4 固件侧，A4；W5 对应物）
 *
 * 契约真源：protocol/transport.md §5（冻结）、docs/INTERFACES.md §5/§6；
 * Python 侧同语义对照：bridge/transports/wss/link.py（退避/错误分类/指纹）。
 *
 * 实现说明（骨架边界，诚实标注）：
 *   - 连接/升级/心跳由 esp_websocket_client 1.8.0 承担（成熟实现，不自制加密，
 *     transport.md §5.5）；本文件做：鉴权头注入、text message 聚合（≤16384
 *     冻结上限）、§5.3 错误分类、§5.4 退避重连状态机、NVS 供应读取。
 *   - disable_auto_reconnect=true：重连节奏由本组件按冻结退避驱动
 *     （1/2/4/8/16/30s ±20%，≥60s 稳定重置），不用库默认 10s 固定值。
 *   - CA 验链经 esp-tls cert_pem 生效；SPKI SHA-256 pinning 目前只完成
 *     配置注入点与 fail-closed 供应校验，线上叶子证书 SPKI 比对需要
 *     esp-tls 定制钩子——记 W5 真机项（未验证），见文件尾注释。
 *   - WiFi STA 初始化为桩（P5.2 做省电策略时填充）。
 *   - 仅编译级验证：本文件任何"成功路径"均未上板（P3.3 代理独占板，禁烧录）。
 */
#include "cdt_wss_client.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#define TAG "cdt_wss"

/* ------------------------------------------------------------------ */
/* 冻结退避序列（与 link.py BACKOFF_SEQUENCE_S 数值逐一对应）            */
/* ------------------------------------------------------------------ */

static const uint32_t s_backoff_seq_ms[CDT_WSS_BACKOFF_SEQ_LEN] = CDT_WSS_BACKOFF_SEQ_MS; /* 宏自带花括号 */
static uint32_t s_backoff_attempts;

void cdt_wss_backoff_reset(void)
{
    s_backoff_attempts = 0;
}

uint32_t cdt_wss_backoff_next_ms(void)
{
    uint32_t idx = s_backoff_attempts;
    if (idx >= CDT_WSS_BACKOFF_SEQ_LEN) {
        idx = CDT_WSS_BACKOFF_SEQ_LEN - 1u; /* 封顶 30s 档 */
    }
    s_backoff_attempts++;

    /* ±20% 均匀抖动：r1000 ∈ [-200, 200]（千分比），delay = base*(1000+r)/1000 */
    uint32_t span = 2u * CDT_WSS_BACKOFF_JITTER_PCT * 10u; /* 400 */
    int32_t r1000 = (int32_t)(esp_random() % (span + 1u)) - (int32_t)span / 2;
    int64_t delay = ((int64_t)s_backoff_seq_ms[idx] * (1000 + r1000)) / 1000;
    return (uint32_t)delay;
}

bool cdt_wss_backoff_record_stable(uint32_t held_ms)
{
    if (held_ms >= CDT_WSS_BACKOFF_STABLE_MS) {
        cdt_wss_backoff_reset(); /* 稳定即重置回 1s 档（§5.4） */
        return true;
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* 分类/指纹/NVS 助手                                                   */
/* ------------------------------------------------------------------ */

bool cdt_wss_failure_is_config_error(cdt_wss_failure_t f)
{
    /* 与 link.py CONFIG_ERROR_KINDS 对齐：401/403/证书/1008 四类终态 */
    return f == CDT_WSS_FAIL_AUTH_401 || f == CDT_WSS_FAIL_POLICY_403 ||
           f == CDT_WSS_FAIL_CERT_PIN || f == CDT_WSS_FAIL_AUTH_LOST_1008;
}

const char *cdt_wss_failure_name(cdt_wss_failure_t f)
{
    switch (f) {
    case CDT_WSS_FAIL_RETRYABLE:      return "retryable_backoff";
    case CDT_WSS_FAIL_AUTH_401:       return "auth_rejected_401";
    case CDT_WSS_FAIL_POLICY_403:     return "policy_rejected_403";
    case CDT_WSS_FAIL_CERT_PIN:       return "cert_or_spki_mismatch";
    case CDT_WSS_FAIL_AUTH_LOST_1008: return "auth_state_lost_1008";
    case CDT_WSS_FAIL_NONE:           return "none";
    default:                          return "unknown";
    }
}

void cdt_wss_token_fingerprint8(const char *token, char out[9])
{
    /* 凭证红线：日志只允许 SHA-256 前 8 hex（link.py token_fingerprint8 同语义） */
    if (out == NULL) {
        return;
    }
    out[0] = '\0';
    if (token == NULL) {
        return;
    }
    unsigned char hash[32];
    if (mbedtls_sha256((const unsigned char *)token, strlen(token), hash, 0) != 0) {
        return;
    }
    snprintf(out, 9, "%02x%02x%02x%02x",
             hash[0], hash[1], hash[2], hash[3]);
}

esp_err_t cdt_wss_nvs_read_str(const char *ns, const char *key,
                               char *out, size_t out_cap, size_t *out_len)
{
    if (ns == NULL || key == NULL || out == NULL || out_cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t rc = nvs_open(ns, NVS_READONLY, &h);
    if (rc != ESP_OK) {
        return rc;
    }
    size_t need = 0;
    rc = nvs_get_str(h, key, NULL, &need);
    if (rc != ESP_OK) {
        nvs_close(h);
        return rc;
    }
    if (need > out_cap) {
        nvs_close(h);
        return ESP_ERR_NO_MEM;
    }
    rc = nvs_get_str(h, key, out, &need);
    nvs_close(h);
    if (rc == ESP_OK && out_len != NULL) {
        *out_len = need;
    }
    return rc;
}

/* ------------------------------------------------------------------ */
/* WiFi STA 桩（P5.2 填充；本骨架不初始化网络）                          */
/* ------------------------------------------------------------------ */

esp_err_t cdt_wss_wifi_sta_init_stub(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t cdt_wss_wifi_sta_set_sleep_policy_stub(bool enable)
{
    (void)enable;
    return ESP_ERR_NOT_SUPPORTED;
}

/* ------------------------------------------------------------------ */
/* 客户端状态机                                                          */
/* ------------------------------------------------------------------ */

#define EV_CONNECTED     BIT0
#define EV_DISCONNECTED  BIT1
#define EV_STOP_POLL_MS  200u
#define CONNECT_WAIT_MS  15000u
#define CDT_WSS_TOKEN_MAX 256u

typedef struct {
    volatile bool running;
    volatile bool config_error;      /* 终态闩锁：仅 cdt_wss_config_error_clear 解锁 */
    volatile int  close_code_pending;/* 聚合超限 → 由任务发 close 1009（§5.3） */
    int64_t connected_since_ms;
    cdt_wss_failure_t last_failure;
    int last_detail;
    bool was_connected;
} wss_ctrl_t;

static wss_ctrl_t s_ctl;
static cdt_wss_config_t s_cfg;                 /* 浅拷贝；字符串/缓冲由调用方持有 */
static bool s_started;
static esp_websocket_client_handle_t s_client;
static TaskHandle_t s_task;
static EventGroupHandle_t s_ev;
static cdt_wss_stats_t s_stats;

static uint8_t s_agg[CDT_WSS_MAX_DOWNLINK + 1]; /* 聚合缓冲（16KiB+1 防越界写） */
static size_t s_agg_len;
static int s_agg_total;
static bool s_agg_aborted; /* 超限后弃收残余分片：不解析不应用（§5.3） */
static char s_auth_hdr[CDT_WSS_TOKEN_MAX + 32]; /* "Authorization: Bearer <token>" */

static void report(cdt_wss_link_state_t st, cdt_wss_failure_t f, int detail)
{
    if (s_cfg.on_link != NULL) {
        s_cfg.on_link(s_cfg.user, st, f, detail);
    }
}

/* §5.3：聚合 >16384 → 记录计数、关闭重连（退避），不解析不应用。
 * close 不能在事件回调内调用（esp_websocket_client 约束）→ 挂起给任务。 */
static void oversize_defer_close(void)
{
    s_stats.rx_oversize_count++;
    s_agg_len = 0;
    s_agg_total = 0;
    s_agg_aborted = true; /* 弃收本消息残余分片（防半包误交付） */
    if (s_ctl.close_code_pending == 0) {
        s_ctl.close_code_pending = CDT_WSS_CLOSE_TOO_BIG;
    }
    ESP_LOGW(TAG, "下行聚合超 16KiB（计数=%" PRIu32 "），挂起 close 1009",
             s_stats.rx_oversize_count);
}

/* text message 聚合：WEBSOCKET_EVENT_DATA 按 payload_offset/payload_len 分片
 * 到达；fin 或凑满 payload_len 视为一条完整消息（只回调完整包，绝不半包）。 */
static void handle_ws_data(const esp_websocket_event_data_t *e)
{
    if (e->op_code != 0x1 && e->op_code != 0x0) {
        return; /* 只收 text/continuation；ping/pong/close 由库处理 */
    }
    if (e->payload_offset == 0) {
        s_agg_aborted = false; /* 新消息开始：解除弃收 */
        s_agg_len = 0;
        s_agg_total = e->payload_len;
        if (s_agg_total > (int)CDT_WSS_MAX_DOWNLINK) {
            oversize_defer_close();
            return;
        }
    } else if (s_agg_aborted) {
        return; /* 超限消息的残余分片：丢弃，绝不交付 */
    }
    if (e->data_len > 0) {
        if (s_agg_len + (size_t)e->data_len > CDT_WSS_MAX_DOWNLINK) {
            oversize_defer_close();
            return;
        }
        memcpy(&s_agg[s_agg_len], e->data_ptr, (size_t)e->data_len);
        s_agg_len += (size_t)e->data_len;
    }
    bool complete = (e->payload_len > 0) &&
                    (e->fin || (e->payload_offset + e->data_len) >= e->payload_len);
    if (complete && s_agg_len > 0) {
        /* 库在分片消息后会补一帧零长 FIN 边界帧：deliver 后立即清零防重发 */
        size_t len = s_agg_len;
        s_agg_len = 0;
        s_stats.rx_message_count++;
        if (s_cfg.on_text != NULL) {
            s_cfg.on_text(s_cfg.user, s_agg, len);
        }
    }
}

static void on_ws_event(void *arg, esp_event_base_t base, int32_t id, void *event_data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *e = (esp_websocket_event_data_t *)event_data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        s_stats.connect_count++;
        s_ctl.connected_since_ms = esp_timer_get_time() / 1000LL;
        s_ctl.was_connected = true;
        ESP_LOGI(TAG, "升级成功（101），connected");
        report(CDT_WSS_LINK_CONNECTED, CDT_WSS_FAIL_NONE, 0);
        xEventGroupSetBits(s_ev, EV_CONNECTED);
        break;

    case WEBSOCKET_EVENT_DISCONNECTED: {
        /* §5.3：1008 → CONFIG_ERROR；1000/1006/1009/1011/未列明 → 可重试。
         * close_status_code=0（无 close frame 的异常断链）按 1006 语义。 */
        int code = e ? e->close_status_code : 0;
        s_ctl.last_failure = (code == CDT_WSS_CLOSE_POLICY)
                                 ? CDT_WSS_FAIL_AUTH_LOST_1008
                                 : CDT_WSS_FAIL_RETRYABLE;
        s_ctl.last_detail = code;
        ESP_LOGW(TAG, "断开 close=%d classified=%s", code,
                 cdt_wss_failure_name(s_ctl.last_failure));
        xEventGroupSetBits(s_ev, EV_DISCONNECTED);
        break;
    }

    case WEBSOCKET_EVENT_ERROR: {
        esp_websocket_error_codes_t *eh = e ? &e->error_handle : NULL;
        int status = eh ? eh->esp_ws_handshake_status_code : 0;
        cdt_wss_failure_t f = CDT_WSS_FAIL_RETRYABLE;
        /* 顺序：401/403（§5.2 第 2 步）→ 证书/SPKI（§5.5）→ 其余可重试 */
        if (status == 401) {
            f = CDT_WSS_FAIL_AUTH_401;
        } else if (status == 403) {
            f = CDT_WSS_FAIL_POLICY_403;
        } else if (eh != NULL && eh->esp_tls_cert_verify_flags != 0) {
            f = CDT_WSS_FAIL_CERT_PIN;
        } else if (eh != NULL &&
                   eh->error_type == WEBSOCKET_ERROR_TYPE_PONG_TIMEOUT) {
            f = CDT_WSS_FAIL_RETRYABLE; /* pong 超时按 1006 语义（§5.2 第 5 步） */
        }
        s_ctl.last_failure = f;
        s_ctl.last_detail = status;
        ESP_LOGW(TAG, "错误事件：type=%d handshake_status=%d tls_flags=%d → %s",
                 eh ? (int)eh->error_type : -1, status,
                 eh ? eh->esp_tls_cert_verify_flags : 0,
                 cdt_wss_failure_name(f));
        xEventGroupSetBits(s_ev, EV_DISCONNECTED);
        break;
    }

    case WEBSOCKET_EVENT_DATA:
        handle_ws_data(e);
        break;

    case WEBSOCKET_EVENT_CLOSED:
        xEventGroupSetBits(s_ev, EV_DISCONNECTED);
        break;

    default:
        break;
    }
}

/* 组建一个 client 实例（fail-closed 校验已在 start 做过；这里组装参数）。 */
static esp_websocket_client_handle_t build_client(void)
{
    esp_websocket_client_config_t wc;
    memset(&wc, 0, sizeof(wc));
    wc.uri = s_cfg.uri;
    wc.buffer_size = (int)(s_cfg.buffer_size ? s_cfg.buffer_size : 2048u);
    wc.disable_auto_reconnect = true; /* 重连节奏归本组件（冻结退避） */
    wc.network_timeout_ms = 10000;
    wc.ping_interval_sec = 20;        /* §5.2：与 Bridge 心跳节奏对齐 20s/20s */
    wc.pingpong_timeout_sec = 20;
    wc.task_prio = 4;
    if (s_cfg.ca_pem != NULL) {
        wc.cert_pem = s_cfg.ca_pem;   /* ①CA 验链（SPKI pin 见文件尾 W5 注释） */
    }
    if (s_cfg.token != NULL) {
        wc.headers = s_auth_hdr;      /* ②Authorization: Bearer（不落日志） */
    }
    return esp_websocket_client_init(&wc);
}

static void delay_backoff(uint32_t delay_ms)
{
    /* 分片睡眠便于 stop 即时打断 */
    uint32_t waited = 0;
    while (s_ctl.running && waited < delay_ms) {
        uint32_t slice = delay_ms - waited;
        if (slice > EV_STOP_POLL_MS) {
            slice = EV_STOP_POLL_MS;
        }
        vTaskDelay(pdMS_TO_TICKS(slice));
        waited += slice;
    }
}

/* 单轮连接：建立 → 等待 → （已连接期）等待断开/超限 close → 分类收尾。 */
static void connect_once(void)
{
    s_agg_len = 0;
    s_agg_total = 0;                 /* 旧链路半包丢弃（§5.4） */
    s_ctl.last_failure = CDT_WSS_FAIL_RETRYABLE;
    s_ctl.last_detail = 0;
    s_ctl.close_code_pending = 0;
    s_ctl.was_connected = false;
    xEventGroupClearBits(s_ev, EV_CONNECTED | EV_DISCONNECTED);

    report(CDT_WSS_LINK_CONNECTING, CDT_WSS_FAIL_NONE, 0);

    s_client = build_client();
    if (s_client == NULL) {
        s_ctl.last_failure = CDT_WSS_FAIL_RETRYABLE; /* 资源失败可重试 */
        ESP_LOGE(TAG, "esp_websocket_client_init 失败");
    } else {
        esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY,
                                      on_ws_event, NULL);
        esp_websocket_client_start(s_client);
    }

    /* 等待连接建立或失败（分片轮询 stop）；init 失败直接进可重试退避 */
    int waited = 0;
    EventBits_t bits = 0;
    while (s_ctl.running && s_client != NULL && waited < CONNECT_WAIT_MS) {
        bits = xEventGroupGetBits(s_ev);
        if (bits & (EV_CONNECTED | EV_DISCONNECTED)) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(EV_STOP_POLL_MS));
        waited += EV_STOP_POLL_MS;
    }

    if (s_ctl.running && (bits & EV_CONNECTED) && !(bits & EV_DISCONNECTED)) {
        /* 已连接期：等待断开或处理挂起的 oversize close */
        while (s_ctl.running) {
            bits = xEventGroupWaitBits(s_ev, EV_DISCONNECTED, pdTRUE,
                                       pdFALSE, pdMS_TO_TICKS(EV_STOP_POLL_MS));
            if (!s_ctl.running) {
                break;
            }
            if (s_ctl.close_code_pending != 0) {
                /* §5.3：>16KiB → close 1009 后走退避重连；Bridge 侧 1009 对上行同理 */
                esp_websocket_client_close_with_code(
                    s_client, s_ctl.close_code_pending, NULL, 0,
                    pdMS_TO_TICKS(2000));
                s_ctl.close_code_pending = 0;
            }
            if (bits & EV_DISCONNECTED) {
                break;
            }
        }
    }

    if (s_client != NULL) {
        if (s_ctl.running) {
            esp_websocket_client_close(s_client, pdMS_TO_TICKS(1000));
        }
        esp_websocket_client_stop(s_client);
        esp_websocket_client_destroy(s_client);
        s_client = NULL;
    }

    if (!s_ctl.running) {
        return;
    }

    cdt_wss_failure_t f = s_ctl.last_failure;
    int detail = s_ctl.last_detail;

    if (cdt_wss_failure_is_config_error(f)) {
        /* 终态：闩锁，只等配置变更/人工 clear（§5.4 禁高频无限重试） */
        s_ctl.config_error = true;
        report(CDT_WSS_LINK_CONFIG_ERROR, f, detail);
        return;
    }

    /* 可重试：稳定保持 ≥60s → 重置退避；否则推进档位并等待（±20% 抖动） */
    if (s_ctl.was_connected) {
        int64_t held = esp_timer_get_time() / 1000LL - s_ctl.connected_since_ms;
        if (cdt_wss_backoff_record_stable((uint32_t)held)) {
            ESP_LOGI(TAG, "连接保持 %" PRIi64 "ms ≥60s，退避重置", held);
        }
    }
    uint32_t delay = cdt_wss_backoff_next_ms();
    report(CDT_WSS_LINK_BACKOFF_WAIT, f, detail);
    ESP_LOGW(TAG, "%u ms 后重试（%s detail=%d）", (unsigned)delay,
             cdt_wss_failure_name(f), detail);
    delay_backoff(delay);
}

static void wss_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "WSS 客户端任务启动（骨架）");
    while (s_ctl.running) {
        if (s_ctl.config_error) {
            /* 终态闩锁：低频驻留，绝不重试（等 cdt_wss_config_error_clear） */
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        connect_once();
    }
    ESP_LOGI(TAG, "WSS 客户端任务退出");
    s_task = NULL;
    vTaskDelete(NULL);
}

/* ------------------------------------------------------------------ */
/* 生命周期 API                                                          */
/* ------------------------------------------------------------------ */

static bool uri_is_plaintext(const char *uri)
{
    return strncasecmp(uri, "ws://", 5) == 0;
}

static bool host_is_loopback(const char *uri)
{
    /* dev 明文仅限 loopback（§5.1；与 link.py _LOOPBACK_HOSTS 一致） */
    const char *p = strstr(uri, "://");
    if (p == NULL) {
        return false;
    }
    p += 3;
    return strncmp(p, "127.0.0.1", 9) == 0 ||
           strncasecmp(p, "[::1]", 5) == 0 ||
           strncasecmp(p, "::1", 3) == 0 ||
           strncasecmp(p, "localhost", 9) == 0;
}

esp_err_t cdt_wss_start(const cdt_wss_config_t *cfg)
{
    if (cfg == NULL || cfg->uri == NULL || cfg->on_text == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    /* fail-closed（§5.1/§5.5）：明文 ws 仅 dev loopback 显式标记；
     * 生产 wss 必须有 CA 与 SPKI pin 供应（线上 SPKI 比对钩子属 W5 未验证项）。 */
    if (uri_is_plaintext(cfg->uri)) {
        if (!cfg->allow_insecure_ws) {
            return ESP_ERR_INVALID_ARG;
        }
        if (!host_is_loopback(cfg->uri)) {
            return ESP_ERR_INVALID_ARG; /* 明文仅 127.0.0.1/::1/localhost */
        }
    } else if (cfg->ca_pem == NULL || cfg->spki_sha256 == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    s_cfg = *cfg; /* 浅拷贝：字符串与缓冲生命周期由调用方保证（至 stop 返回） */
    if (s_cfg.token != NULL) {
        snprintf(s_auth_hdr, sizeof(s_auth_hdr), "Authorization: Bearer %s",
                 s_cfg.token);
        char fp[9];
        cdt_wss_token_fingerprint8(s_cfg.token, fp);
        ESP_LOGI(TAG, "WSS start：uri=%s token 指纹=%s…（内容不落日志）",
                 s_cfg.uri, fp);
    } else {
        s_auth_hdr[0] = '\0';
        ESP_LOGI(TAG, "WSS start：uri=%s（无 token——401 终态路径可用）", s_cfg.uri);
    }

    memset(&s_ctl, 0, sizeof(s_ctl));
    s_ctl.running = true;
    memset(&s_agg, 0, sizeof(s_agg));
    s_agg_len = 0;
    s_agg_total = 0;
    cdt_wss_backoff_reset();

    s_ev = xEventGroupCreate();
    if (s_ev == NULL) {
        s_ctl.running = false;
        return ESP_ERR_NO_MEM;
    }
    xEventGroupClearBits(s_ev, EV_CONNECTED | EV_DISCONNECTED);

    if (xTaskCreate(wss_task, "cdt_wss", 6144, NULL, 4, &s_task) != pdPASS) {
        vEventGroupDelete(s_ev);
        s_ev = NULL;
        s_ctl.running = false;
        return ESP_ERR_NO_MEM;
    }
    s_started = true;
    return ESP_OK;
}

esp_err_t cdt_wss_stop(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    s_ctl.running = false; /* 任务在 ≤200ms 分片内退出并 destroy client */
    int guard = 0;
    while (s_task != NULL && guard < 30) { /* ≤6s 兜底 */
        vTaskDelay(pdMS_TO_TICKS(EV_STOP_POLL_MS));
        guard++;
    }
    s_started = false;
    s_ctl.config_error = false;
    if (s_task == NULL) {
        /* 任务已清理完毕，事件组可安全回收 */
        if (s_ev != NULL) {
            vEventGroupDelete(s_ev);
            s_ev = NULL;
        }
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT; /* 6s 内未退出（骨架防御路径；不删组防竞态） */
}

esp_err_t cdt_wss_send(const uint8_t *bytes, size_t len)
{
    /* 上行独立上限 512B（§1）；>512 记录计数并拒绝（§5.3 对上行同语义） */
    if (bytes == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len > CDT_WSS_MAX_UPLINK) {
        s_stats.tx_oversize_count++;
        return ESP_ERR_INVALID_SIZE;
    }
    if (s_client == NULL || !esp_websocket_client_is_connected(s_client)) {
        return ESP_ERR_INVALID_STATE; /* busy */
    }
    int rc = esp_websocket_client_send_text(s_client, (const char *)bytes,
                                            (int)len, pdMS_TO_TICKS(1000));
    return (rc >= 0) ? ESP_OK : ESP_ERR_INVALID_STATE;
}

esp_err_t cdt_wss_config_error_clear(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ctl.config_error) {
        return ESP_OK;
    }
    s_ctl.config_error = false; /* 仅配置变更/人工介入后调用（§5.4） */
    cdt_wss_backoff_reset();
    return ESP_OK;
}

cdt_wss_stats_t cdt_wss_get_stats(void)
{
    return s_stats;
}

/* ---------------------------------------------------------------------------
 * W5 真机遗留项（未验证，2026-09-10 骨架交付时点）：
 *   1. SPKI SHA-256 pinning 的线上强制：esp_websocket_client/esp-tls 未暴露
 *      对端证书句柄，叶子证书 SPKI 摘要比对需 esp-tls 定制（mbedtls verify
 *      回调或 crt_bundle 公钥哈希 bundle）。本骨架完成：pin 供应注入点、
 *      fail-closed（无 CA/pin 拒绝启动）、CA 验链（cert_pem 生效）。
 *   2. WiFi STA 初始化与省电策略（P5.2）。
 *   3. 401/403/TLS 失败/1008 分类路径与退避序列的真机回归
 *      （分类逻辑与 tests/transport/wss 的 link.py 断言同源对齐）。
 * --------------------------------------------------------------------------- */
