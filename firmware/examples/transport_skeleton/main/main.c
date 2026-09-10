/*
 * main.c — transport_skeleton 独立工程的接线示例（P3.3/P3.4 固件侧，A4）
 *
 * 目的：把 firmware/components/transport 的 WSS 客户端骨架与 BLE peripheral
 * 骨架编入同一构建目标并完成最小接线（编译级验证；不烧录、不上板）。
 *
 * 上层组装边界（AGENTS.md/INTERFACES §5）：
 *   - 消息→StateStore 的组装归本层（main）；transport 组件只回调完整消息
 *     字节与三值结果。本骨架的 on_message 仅计数并返回 applied——真实
 *     StateStore 接线属后续 main 集成（P5 域），此处不冒充已接入。
 *   - 凭证（token/CA/SPKI pin）自 NVS "cdt_prov" 命名空间读取（首次烧录
 *     供应阶段写入，transport.md §5.5）；未供应时 WSS 不启动、只留日志——
 *     不硬编码任何凭证，不伪造"已连接"。
 */
#include <inttypes.h>
#include <stdint.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "cdt_ble_peripheral.h"
#include "cdt_wss_client.h"

#define TAG "cdt_skel"

#define PROV_NS       "cdt_prov"
#define PROV_KEY_URI  "uri"
#define PROV_KEY_TOK  "token"
#define PROV_KEY_CA   "ca_pem"

/* 供应缓冲（static：cdt_wss_start 浅拷贝配置，字符串须活到 stop） */
static char s_uri[128];
static char s_token[256];
static char s_ca_pem[2048];
static uint8_t s_spki[32];

static volatile uint32_t s_ble_msg_count;
static volatile uint32_t s_wss_msg_count;

/* ------------------------------------------------------------------ */
/* BLE 回调（骨架：计数/日志；数字比较默认拒绝，B2 真机接 KEY 长按）      */
/* ------------------------------------------------------------------ */

static cdt_apply_result_t on_ble_message(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    s_ble_msg_count++;
    ESP_LOGI(TAG, "BLE on_message：完整消息 %u B（计数=%" PRIu32 "）→ applied",
             (unsigned)len, s_ble_msg_count);
    /* 真实接线：此处调 StateStore.apply_json 并回 applied/duplicate/rejected */
    return CDT_APPLY_APPLIED;
}

static void on_ble_link(void *user, bool connected)
{
    (void)user;
    ESP_LOGI(TAG, "BLE link %s", connected ? "connected" : "disconnected");
}

static void on_ble_passkey(void *user, uint32_t passkey)
{
    (void)user;
    ESP_LOGI(TAG, "配对数字比较：屏显 passkey=%06" PRIu32 "（待 KEY 长按确认）",
             passkey);
}

static bool on_ble_numcmp_confirm(void *user, uint32_t passkey)
{
    (void)user;
    (void)passkey;
    /* B2 真机项：接 KEY 长按（GPIO）确认；骨架默认拒绝——不静默放行配对 */
    ESP_LOGW(TAG, "数字比较确认未接线（B2 真机项），默认拒绝");
    return false;
}

/* ------------------------------------------------------------------ */
/* WSS 回调                                                             */
/* ------------------------------------------------------------------ */

static void on_wss_text(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    (void)bytes;
    s_wss_msg_count++;
    /* 只见字节：长度与计数可记日志，内容不打印（payload 可能含业务快照） */
    ESP_LOGI(TAG, "WSS on_text：完整消息 %u B（计数=%" PRIu32 "）",
             (unsigned)len, s_wss_msg_count);
}

static void on_wss_link(void *user, cdt_wss_link_state_t st,
                        cdt_wss_failure_t f, int detail)
{
    (void)user;
    ESP_LOGI(TAG, "WSS link=%d failure=%s detail=%d", (int)st,
             cdt_wss_failure_name(f), detail);
}

/* 从 NVS 读 32B SPKI pin blob（供应阶段写入，transport.md §5.5）。 */
static bool load_spki_pin(void)
{
    nvs_handle_t h;
    if (nvs_open(PROV_NS, NVS_READONLY, &h) != ESP_OK) {
        return false;
    }
    size_t len = sizeof(s_spki);
    esp_err_t rc = nvs_get_blob(h, "spki_pin", s_spki, &len);
    nvs_close(h);
    return rc == ESP_OK && len == 32;
}

/* ------------------------------------------------------------------ */
/* app_main：NVS → BLE（常开）→ WSS（有供应才启动）                       */
/* ------------------------------------------------------------------ */

void app_main(void)
{
    esp_err_t rc = nvs_flash_init();
    if (rc == ESP_ERR_NVS_NO_FREE_PAGES || rc == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    /* ① BLE GATT peripheral（骨架常开；配对/MTU/大包为 B0–B5 真机项） */
    static const cdt_ble_cbs_t ble_cbs = {
        .user               = NULL,
        .on_message         = on_ble_message,
        .on_link            = on_ble_link,
        .on_passkey_display = on_ble_passkey,
        .on_numcmp_confirm  = on_ble_numcmp_confirm,
    };
    rc = cdt_ble_periph_start(&ble_cbs);
    ESP_LOGI(TAG, "cdt_ble_periph_start rc=%s", esp_err_to_name(rc));

    /* ② WiFi STA 桩接口（P5.2 填充省电策略；当前返回 NOT_SUPPORTED） */
    rc = cdt_wss_wifi_sta_init_stub();
    ESP_LOGI(TAG, "cdt_wss_wifi_sta_init_stub rc=%s（桩）", esp_err_to_name(rc));

    /* ③ WSS 客户端：仅在 NVS 供应齐备时启动（fail-closed；无凭证不硬编码） */
    size_t ca_len = 0;
    bool have_uri = cdt_wss_nvs_read_str(PROV_NS, PROV_KEY_URI, s_uri,
                                         sizeof(s_uri), NULL) == ESP_OK;
    bool have_tok = cdt_wss_nvs_read_str(PROV_NS, PROV_KEY_TOK, s_token,
                                         sizeof(s_token), NULL) == ESP_OK;
    bool have_ca = cdt_wss_nvs_read_str(PROV_NS, PROV_KEY_CA, s_ca_pem,
                                        sizeof(s_ca_pem), &ca_len) == ESP_OK;
    bool have_spki = load_spki_pin();

    if (have_uri && have_tok && have_ca && have_spki) {
        cdt_wss_config_t wcfg;
        memset(&wcfg, 0, sizeof(wcfg));
        wcfg.user             = NULL;
        wcfg.on_text          = on_wss_text;
        wcfg.on_link          = on_wss_link;
        wcfg.uri              = s_uri;
        wcfg.token            = s_token;
        wcfg.ca_pem           = s_ca_pem;
        wcfg.spki_sha256      = s_spki;  /* 注入点；线上比对为 W5 未验证项 */
        wcfg.allow_insecure_ws = false;  /* 生产构建禁止明文（§5.1） */
        rc = cdt_wss_start(&wcfg);
        ESP_LOGI(TAG, "cdt_wss_start rc=%s", esp_err_to_name(rc));
    } else {
        /* 未供应：不启动 WSS，保持断言"配置错误可观测"而非伪造连接 */
        ESP_LOGW(TAG, "NVS 未供应 uri/token/ca_pem/spki——WSS 不启动（编译骨架）");
    }

    ESP_LOGI(TAG, "transport_skeleton 接线完成（编译级验证，不上板）");
}
