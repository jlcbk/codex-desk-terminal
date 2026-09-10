/*
 * cdt_wss_client.h — WSS 设备客户端骨架（P3.3/P3.4 固件侧，A4；W5 对应物）
 *
 * 契约真源：protocol/transport.md §5（Wi-Fi/WSS，P0.5 冻结）、
 * docs/INTERFACES.md §5 Transport 行 / §6 Wi-Fi 适配。
 * Python 侧同语义实现：bridge/transports/wss/link.py（错误分类与退避冻结常量
 * 的对照真源——数值必须一致，不允许两处漂移）。
 *
 * 本组件为骨架：连接/鉴权/聚合校验/错误分类/退避接线已成形，
 * SPKI pinning 的线上强制与 WiFi STA 省电策略留桩（P5.2 / W5 真机验证）。
 * 红线（transport.md §5 红线段）：
 *   - 只搬字节：on_text 只交付完整 text message（聚合后 ≤16384B），
 *     不解析任何 AppState/Telemetry 字段；
 *   - 凭证（device token、CA 私钥）不入 State/fixtures/日志——日志只允许
 *     token 的 SHA-256 指纹前 8 hex（cdt_wss_token_fingerprint8）；
 *   - 401/403/证书失败/close 1008 = CONFIG_ERROR 终态，不进入退避循环，
 *     仅配置变更（cdt_wss_config_error_clear）或人工介入后重试。
 */
#ifndef CDT_WSS_CLIENT_H
#define CDT_WSS_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 冻结常量（复述 transport.md §1/§5，不改数值）                        */
/* ------------------------------------------------------------------ */

#define CDT_WSS_PATH "/v1/state"          /* 业务路径冻结（§5.1） */
#define CDT_WSS_MAX_DOWNLINK 16384u       /* 下行聚合上限：AppState ≤16KiB */
#define CDT_WSS_MAX_UPLINK 512u           /* 上行聚合上限：Telemetry ≤512B */

/* 退避序列 1/2/4/8/16/30s（§5.4），±20% 均匀抖动，≥60s 稳定重置回 1s 档
 * ——与 link.py BACKOFF_SEQUENCE_S / BACKOFF_JITTER_FRACTION /
 * BACKOFF_STABLE_RESET_S 逐一对应。 */
#define CDT_WSS_BACKOFF_SEQ_MS { 1000u, 2000u, 4000u, 8000u, 16000u, 30000u }
#define CDT_WSS_BACKOFF_SEQ_LEN 6u
#define CDT_WSS_BACKOFF_JITTER_PCT 20u    /* ±20% */
#define CDT_WSS_BACKOFF_STABLE_MS 60000u  /* 保持 ≥60s → 稳定重置 */

/* WebSocket close code（§5.3 错误码表） */
#define CDT_WSS_CLOSE_NORMAL 1000         /* Bridge 主动停机：可重试 */
#define CDT_WSS_CLOSE_ABNORMAL 1006       /* 无 close frame：可重试 */
#define CDT_WSS_CLOSE_POLICY 1008         /* 连接中鉴权态失效：CONFIG_ERROR */
#define CDT_WSS_CLOSE_TOO_BIG 1009        /* 聚合超限：记录计数+退避，不解析 */
#define CDT_WSS_CLOSE_SERVER_ERR 1011     /* 服务器内部错误：可重试 */

/* ------------------------------------------------------------------ */
/* 链路状态与失败分类（映射 transport.md §5.3 错误表）                   */
/* ------------------------------------------------------------------ */

typedef enum {
    CDT_WSS_LINK_DISCONNECTED = 0,
    CDT_WSS_LINK_CONNECTING = 1,
    CDT_WSS_LINK_CONNECTED = 2,
    CDT_WSS_LINK_BACKOFF_WAIT = 3,    /* 可重试失败后的退避等待 */
    CDT_WSS_LINK_CONFIG_ERROR = 4     /* 终态：401/403/证书/1008，等待人工/配置变更 */
} cdt_wss_link_state_t;

typedef enum {
    CDT_WSS_FAIL_NONE = 0,
    CDT_WSS_FAIL_RETRYABLE = 1,       /* 1000/1006/1009/1011、HTTP 500/503、网络失联 → 退避 */
    CDT_WSS_FAIL_AUTH_401 = 2,        /* token 缺失/错误 → CONFIG_ERROR */
    CDT_WSS_FAIL_POLICY_403 = 3,      /* 策略拒绝（设备数超限等）→ CONFIG_ERROR */
    CDT_WSS_FAIL_CERT_PIN = 4,        /* CA 验链/SPKI 指纹不匹配 → CONFIG_ERROR */
    CDT_WSS_FAIL_AUTH_LOST_1008 = 5   /* close 1008 → CONFIG_ERROR */
} cdt_wss_failure_t;

/* CONFIG_ERROR 终态判定（§5.4：不进退避循环）。 */
bool cdt_wss_failure_is_config_error(cdt_wss_failure_t f);
/* 失败类名（日志用短名，不含敏感值）。 */
const char *cdt_wss_failure_name(cdt_wss_failure_t f);

/* oversize 计数（§5.3：聚合超限"记录计数"，不解析不应用）。 */
typedef struct {
    uint32_t rx_oversize_count;   /* 下行聚合 >16384 次数 */
    uint32_t tx_oversize_count;   /* 上行调用 >512 被拒次数 */
    uint32_t rx_message_count;    /* 正常交付 on_text 次数 */
    uint32_t connect_count;       /* 成功升级（101）次数 */
} cdt_wss_stats_t;
cdt_wss_stats_t cdt_wss_get_stats(void);

/* ------------------------------------------------------------------ */
/* 回调与配置                                                           */
/* ------------------------------------------------------------------ */

typedef struct cdt_wss_config {

    void *user; /* 回调上下文（原样回传） */

    /* 完整 text message 交付（聚合后 ≤16384B；仅完整包，绝不回调半包）。
     * bytes 指向组件内部聚合缓冲，仅回调返回前有效。 */
    void (*on_text)(void *user, const uint8_t *bytes, size_t len);

    /* 链路状态通知（含最近失败分类；CONFIG_ERROR 终态亦由此上报）。
     * detail：失败时为 close code / HTTP 状态（无则为 0）。 */
    void (*on_link)(void *user, cdt_wss_link_state_t state,
                    cdt_wss_failure_t failure, int detail);

    /* ---- 连接参数（全部由上层自 NVS 供应值填入，组件不持久化） ---- */
    const char *uri;         /* 生产 wss://host:port/v1/state（§5.1/§5.5） */
    const char *token;       /* 设备 token（Bearer；非 NULL 时自动加头） */
    const char *ca_pem;      /* 预置自签 CA（PEM，NUL 结尾）；NULL=不合法（生产拒绝） */
    const uint8_t *spki_sha256; /* 叶子证书 SPKI SHA-256 pin，32B（注入点） */

    uint32_t buffer_size;    /* 单帧接收缓冲，默认/0 取 2048（聚合上限另算 16KiB） */
    bool allow_insecure_ws;  /* 明文 ws://：仅 dev loopback 构建可置 true（§5.1），
                              * 生产构建必须为 false，置 true 即拒绝启动。 */
} cdt_wss_config_t;

/* 启动客户端（异步：内部任务负责连接/重连）。uri 为空、生产配置缺
 * ca_pem/pin、或 allow_insecure_ws 非法时返回 ESP_ERR_INVALID_ARG（fail-closed）。 */
esp_err_t cdt_wss_start(const cdt_wss_config_t *cfg);

/* 停止并释放（关连接、清退避与聚合缓冲）。电池保护（CRITICAL）路径调用。 */
esp_err_t cdt_wss_stop(void);

/* 上行遥测（≤512B，独立 text message）。>512 拒绝并计 tx_oversize_count。
 * 返回 ESP_OK=accepted / ESP_ERR_INVALID_STATE=busy（未连接）/ 其他=error。 */
esp_err_t cdt_wss_send(const uint8_t *bytes, size_t len);

/* CONFIG_ERROR 终态解锁（仅配置变更/人工介入后调用；§5.4 禁止高频无限重试）。 */
esp_err_t cdt_wss_config_error_clear(void);

/* ------------------------------------------------------------------ */
/* 退避状态机（与 link.py BackoffPolicy 同语义；公开供虚拟时钟断言对齐） */
/* ------------------------------------------------------------------ */

void cdt_wss_backoff_reset(void);            /* 人工重置回 1s 档 */
uint32_t cdt_wss_backoff_next_ms(void);      /* 下一档时长（含 ±20% 抖动） */
/* 连接实际保持 ≥60s → 稳定，重置退避；返回是否发生重置。 */
bool cdt_wss_backoff_record_stable(uint32_t held_ms);

/* ------------------------------------------------------------------ */
/* 供应读取助手与日志安全指纹（凭证红线）                                */
/* ------------------------------------------------------------------ */

/* 从 NVS 读字符串供应项（token / CA PEM；NVS 首次烧录阶段写入，§5.5）。
 * out 以 NUL 结尾；buf 不足返回 ESP_ERR_NO_MEM；键不存在返回 ESP_ERR_NVS_NOT_FOUND。 */
esp_err_t cdt_wss_nvs_read_str(const char *ns, const char *key,
                               char *out, size_t out_cap, size_t *out_len);

/* token 的日志安全指纹：SHA-256 前 8 hex 写 out（out_cap ≥9，含 NUL）。 */
void cdt_wss_token_fingerprint8(const char *token, char out[9]);

/* ------------------------------------------------------------------ */
/* WiFi STA 桩（P5.2 做省电策略时填充；本骨架不初始化网络）              */
/* ------------------------------------------------------------------ */

esp_err_t cdt_wss_wifi_sta_init_stub(void);       /* 桩：返回 ESP_ERR_NOT_SUPPORTED */
esp_err_t cdt_wss_wifi_sta_set_sleep_policy_stub(bool enable); /* 桩：同上 */

#ifdef __cplusplus
}
#endif

#endif /* CDT_WSS_CLIENT_H */
