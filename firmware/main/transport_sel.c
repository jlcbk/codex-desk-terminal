/*
 * transport_sel.c — 产品主流程传输选择实现（ZC11）
 *
 * 两种构建形态（同一份源码，Kconfig CDT_TRANSPORT_SEL 编译期二选一）：
 *   SEL=0（默认）：全部接口为常量折叠 no-op，不 include 任何 BLE 头——
 *                 WIFI 构建不引用 ble_peripheral 符号，链接产物与接入前
 *                 等价（回归零变化）；WiFi/WSS 启动仍由 main.c 持有。
 *   SEL=1       ：不启动 WiFi/WSS（main.c 调用点按 is_ble() 分流）；本模块
 *                 启动 cdt_ble_peripheral 并把收包接进与 WSS 同一 app_inbox
 *                 下游。分片→重组→CRC 全部发生在组件内（ble_peripheral.c
 *                 内部已接 shared/transport 引擎），本模块只见完整包。
 *
 * ACK 语义说明（记录给 A0 真机收编段，transport.md §3.4）：
 *   冻结契约要求 ACK 依据 Store 层三值结果（applied/duplicate/rejected）。
 *   设备侧 StateStore 只允许 app 主任务触碰（单解析任务边界，R4），而 BLE
 *   on_message 回调运行在 NimBLE host 任务上下文——无法同步取得三值而不
 *   破坏该边界。本接线选择「入槽 + 乐观 ACK（CDT_APPLY_APPLIED）」：
 *   duplicate 场景按 §3.4 本就应 ACK（不重复渲染由 store seq 门卫保证）；
 *   偏差仅在 rejected（CRC 已过但 JSON 非法）不回 NACK、由主任务解析日志
 *   暴露（[state] REJECTED）。若真机联调要求严格 §3.4，须 A0 裁决跨任务
 *   三值回传设计后再改（本模块单点可改）。
 *
 * 验证状态：编译级（两配置 idf.py build）。广播/配对/MTU/大包/断连半包
 * 均为 P3.4 B 系列真机项（板被并行任务占用，本轮未验证）。
 */
#include "transport_sel.h"

#include <inttypes.h>

#include "sdkconfig.h"

#include "esp_err.h"
#include "esp_log.h"

#include "app_inbox.h" /* 与 WSS on_text 同一收件槽下游（R4 语义不变） */

#if CONFIG_CDT_TRANSPORT_SEL == 1

#include "cdt_ble_peripheral.h" /* 冻结 UUID/广播名常量与外设 API（组件 transport） */

static const char *TAG = "cdt_tsel";

/* BLE 链路镜像（on_link 在 NimBLE host 任务上下文，只写 volatile + 日志） */
static volatile bool s_ble_up;
static bool s_ble_started;

bool cdt_transport_sel_is_ble(void)
{
    return CONFIG_CDT_TRANSPORT_SEL == 1;
}

const char *cdt_transport_sel_name(void)
{
    return CONFIG_CDT_TRANSPORT_SEL == 1 ? "ble" : "wifi";
}

/* 完整消息交付（重组齐包 + CRC32 通过，NimBLE host 任务上下文）：
 * 与 WSS on_text 完全同一 app_inbox 管线下游——互斥短临界区入槽、
 * 优先提醒槽判定（needs_you/error）与消费者语义全部不变（app_inbox.c 单源）。
 * 返回值驱动组件自动 ACK/NACK（§3.4；乐观 ACK 偏差见文件头说明）。 */
static cdt_apply_result_t ble_on_message(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    app_inbox_produce(bytes, len);
    return CDT_APPLY_APPLIED;
}

static void ble_on_link(void *user, bool connected)
{
    (void)user;
    s_ble_up = connected;
    /* 断连时组件内部已完成 cdt_reassembler_reset 与广播重启（§3.3 接收方 6）；
     * 上层 store 保留旧快照，重连后 Bridge 重发全量（§1）。 */
    ESP_LOGI(TAG, "[ble] link=%s", connected ? "connected" : "disconnected");
}

static void ble_on_passkey(void *user, uint32_t passkey)
{
    (void)user;
    /* §2.3 数字比较：屏显 6 位 passkey 的上屏位接线归真机联调段（A0）；
     * 本阶段串口日志取证。KEY 长按确认未接线 → on_numcmp_confirm 传 NULL，
     * 组件缺省拒绝（绝不静默退化 Just Works/公开写入）。 */
    ESP_LOGW(TAG,
             "[ble] 配对数字比较 passkey=%06" PRIu32 "（KEY 确认未接线，B2 真机项）",
             passkey);
}

void cdt_transport_sel_ble_start(void)
{
    if (s_ble_started) {
        return; /* 幂等（运行期 ALLOW_RADIO_START 可能重复触发） */
    }
    static const cdt_ble_cbs_t cbs = {
        .user = NULL,
        .on_message = ble_on_message,
        .on_link = ble_on_link,
        .on_passkey_display = ble_on_passkey,
        .on_numcmp_confirm = NULL, /* 缺省拒绝；KEY 接线归 B2 真机取证 */
    };
    esp_err_t err = cdt_ble_periph_start(&cbs);
    if (err == ESP_OK) {
        s_ble_started = true;
        ESP_LOGI(TAG, "[ble] peripheral 启动（svc=" CDT_BLE_SVC_UUID_STR
                      " name=" CDT_BLE_ADV_NAME "）");
    } else {
        ESP_LOGE(TAG, "[ble] peripheral 启动失败: %s", esp_err_to_name(err));
    }
}

void cdt_transport_sel_radio_stop(void)
{
    if (!s_ble_started) {
        return;
    }
    esp_err_t err = cdt_ble_periph_stop();
    s_ble_started = false;
    s_ble_up = false;
    ESP_LOGW(TAG, "[ble] radio stop: %s（断连即清半包，§3.3 接收方 6）",
             esp_err_to_name(err));
}

bool cdt_transport_sel_ble_link_up(void)
{
    return s_ble_up;
}

bool cdt_transport_sel_poll(int64_t now_ms)
{
    /* 3s 无进展 / 30s 整包超时巡检（§3.3 接收方 4）；未连接时组件内返回
     * false（半包只存在于连接期）。 */
    return cdt_ble_periph_poll_reassembly(now_ms);
}

#else /* SEL=0：WIFI 默认路径——常量折叠 no-op，不引用任何 BLE 符号 */

bool cdt_transport_sel_is_ble(void)
{
    return false;
}

const char *cdt_transport_sel_name(void)
{
    return "wifi";
}

void cdt_transport_sel_ble_start(void)
{
    /* WIFI 模式不可达（is_ble()==编译期 false） */
}

void cdt_transport_sel_radio_stop(void)
{
    /* WIFI 模式：wss/dev_net 由 main cb_stop_radio 照旧直停（零变化） */
}

bool cdt_transport_sel_ble_link_up(void)
{
    return false;
}

bool cdt_transport_sel_poll(int64_t now_ms)
{
    (void)now_ms;
    return false;
}

#endif /* CONFIG_CDT_TRANSPORT_SEL == 1 */
