/*
 * cdt_ble_peripheral.h — BLE GATT peripheral 骨架（P3.3/P3.4 固件侧，A4；B 系列对应物）
 *
 * 契约真源：protocol/transport.md §2（GATT/配对/MTU，P0.5 冻结）、§3（16B 帧头）、
 * docs/INTERFACES.md §7（BLE 适配）。栈选型：NimBLE（esp-idf bt 组件，
 * CONFIG_BT_NIMBLE_ENABLED）——相对 bluedroid 省内存（GATT/ATT/GAP 单宿主栈、
 * RAM 占用显著更低），且 transport.md §2.2 的 encrypted+MITM 权限映射
 * （BLE_ATT_F_WRITE/WRITE_NO_RSP + encrypted + MITM）在 NimBLE 即
 * BLE_GATT_CHR_F_WRITE/_NO_RSP 加 BLE_GATT_CHR_F_WRITE_ENCRYPTED/_AUTHEN 标志。
 *
 * 接线边界（红线）：
 *   - 本组件只搬字节：RX Write（片=16B 帧头+载荷）喂 shared/transport 的
 *     cdt_reassembler；重组完整包经 on_message 交付上层（消息→StateStore
 *     由上层 main 组装），并以返回的三值结果（cdt_apply_result_t）驱动
 *     ACK/NACK（transport.md §3.4，经 cdt_build_ack/nack + TX Notify 发出）；
 *   - 组件不解析任何 AppState/Telemetry 业务字段；
 *   - 断连回调内部执行 cdt_reassembler_reset（清半包与位图，§3.3 接收方 6）。
 *
 * 验证状态：本骨架仅编译级验证。配对（LE Secure Connections+数字比较+KEY
 * 确认）、MTU 协商、大包吞吐、断连半包均为 P3.4 步骤 B0–B5 真机项（未验证）。
 */
#ifndef CDT_BLE_PERIPHERAL_H
#define CDT_BLE_PERIPHERAL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* 三值结果与 ACK/NACK 构造来自 shared/transport 冻结引擎（P3.4 交付）。 */
#include "cdt_fragmenter.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 冻结 UUID（transport.md §2.1：UUIDv5 一次生成后永不再变）             */
/* ------------------------------------------------------------------ */

#define CDT_BLE_SVC_UUID_STR "B931F216-B7FD-50E9-8C33-F1416ADE3B1D"
#define CDT_BLE_RX_UUID_STR  "890AAC2C-3C19-5200-BDEC-74943BA536A1" /* Write，central→设备 */
#define CDT_BLE_TX_UUID_STR  "8EABE170-A4E7-5C26-A287-6F5871014707" /* Notify/Indicate，设备→central */

#define CDT_BLE_ADV_NAME "CodexDT" /* transport.md §2.2：设备名放 scan response */

/* 128bit UUID 小端字节序（NimBLE BLE_UUID128_INIT 取 LSB-first；
 * 与上方冻结字符串逐字节对齐，B2 真机用 UUID 过滤时以此为准）。 */
#define CDT_BLE_SVC_UUID_LE 0x1D,0x3B,0xDE,0x6A,0x41,0xF1,0x33,0x8C,0xE9,0x50,0xFD,0xB7,0x16,0xF2,0x31,0xB9
#define CDT_BLE_RX_UUID_LE  0xA1,0x36,0xA5,0x3B,0x94,0x74,0xEC,0xBD,0x00,0x52,0x19,0x3C,0x2C,0xAC,0x0A,0x89
#define CDT_BLE_TX_UUID_LE  0x07,0x47,0x01,0x71,0x58,0x6F,0x87,0xA2,0x26,0x5C,0xE7,0xA4,0x70,0xE1,0xAB,0x8E

/* ------------------------------------------------------------------ */
/* 回调                                                                 */
/* ------------------------------------------------------------------ */

typedef struct {
    void *user;

    /* 完整消息交付（分片齐、CRC 通过；bytes 指向重组缓冲，仅回调返回前有效，
     * 需保留请拷贝）。返回三值结果驱动 ACK/NACK（transport.md §3.4）：
     *   CDT_APPLY_APPLIED/CDT_APPLY_DUPLICATE → ACK
     *   CDT_APPLY_REJECTED                    → NACK（对端重发全包）
     * 消息→StateStore 组装在上层 main（组件不接触 store）。 */
    cdt_apply_result_t (*on_message)(void *user, const uint8_t *bytes, size_t len);

    /* 链路通知（connected=true 连接建立；false 断连——断连时组件内部已完成
     * cdt_reassembler_reset 与广播重启，上层只需清理自身会话状态）。 */
    void (*on_link)(void *user, bool connected);

    /* 配对：数字比较（LE Secure Connections）——显示 6 位 passkey（§2.3）。 */
    void (*on_passkey_display)(void *user, uint32_t passkey);

    /* 配对：KEY 长按确认（transport.md §2.3 双向确认；可行性 B2 真机取证，
     * 不可行须按 INTERFACES §7 记录修订设计，不得静默退化 Just Works）。 */
    bool (*on_numcmp_confirm)(void *user, uint32_t passkey);
} cdt_ble_cbs_t;

/* ------------------------------------------------------------------ */
/* 生命周期与发送                                                        */
/* ------------------------------------------------------------------ */

/* 初始化 NimBLE 宿主、注册冻结 GATT 服务并开始广播。
 * 广播内容：Flags + 128bit Service UUID（adv 包）；设备名 CodexDT
 * （scan response，§2.2）。安全：SC+MITM+绑定，IO cap=DISPLAY_YESNO。 */
esp_err_t cdt_ble_periph_start(const cdt_ble_cbs_t *cbs);

/* 反注册并停止广播（transport 切换前由上层先调用，§1）。 */
esp_err_t cdt_ble_periph_stop(void);

/* 经 TX 特征发送 Notify（ACK/NACK 16B 帧由 cdt_build_ack/nack 构造后传入；
 * 组件内部亦自动发送，此接口供上层显式重发场景）。未连接返回 ESP_ERR_INVALID_STATE。 */
esp_err_t cdt_ble_periph_notify(const uint8_t *bytes, size_t len);

/* 当前协商 ATT MTU（连接未建立时为 0）。片容量 = CDT_FRAGMENT_CAPACITY(mtu)。 */
uint16_t cdt_ble_periph_current_mtu(void);

/* 重组器巡检（3s 进展/30s 整包超时；空闲定时器可调用，NACK 由组件发出）。 */
bool cdt_ble_periph_poll_reassembly(int64_t now_ms);

/* 诊断计数（B 系列真机取证用）。 */
typedef struct {
    uint32_t rx_fragment_count;   /* 合法 RX ATT 写入（含重复片） */
    uint32_t rx_short_write_count;/* <16B 短写（ATT 错误应答） */
    uint32_t nack_sent_count;     /* NACK 发出次数 */
    uint32_t ack_sent_count;      /* ACK 发出次数 */
    uint32_t notify_fail_count;   /* TX Notify 失败次数 */
} cdt_ble_stats_t;
cdt_ble_stats_t cdt_ble_get_stats(void);

#ifdef __cplusplus
}
#endif

#endif /* CDT_BLE_PERIPHERAL_H */
