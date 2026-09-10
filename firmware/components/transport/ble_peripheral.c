/*
 * ble_peripheral.c — BLE GATT peripheral 骨架实现（P3.3/P3.4 固件侧，A4；B 系列对应物）
 *
 * 契约真源：protocol/transport.md §2（GATT/配对/MTU）、§3（16B 帧头）、
 * docs/INTERFACES.md §7。栈选型：NimBLE（esp-idf bt 组件，CONFIG_BT_NIMBLE_ENABLED）
 * ——相对 bluedroid 省内存（轻量单宿主栈、GATT/ATT/GAP 无 profile 冗余），
 * 且 transport.md §2.2 的 encrypted+MITM 权限映射即 NimBLE 的
 * BLE_GATT_CHR_F_WRITE_ENCRYPTED / _AUTHEN 特征标志。
 *
 * 接线（红线：只搬字节）：
 *   RX Write（片=16B 帧头+载荷）→ cdt_frame_decode → cdt_reassembler_feed
 *   （共享引擎静态分配 ≈17KiB）；齐包 CRC 通过 → 上层 on_message 三值结果
 *   → cdt_build_ack_nack → TX Notify 回发（§3.4，transport 不读业务字段）。
 *   任一片矛盾/超时 → on_reject → NACK。断连 → cdt_reassembler_reset（清半包）。
 *
 * 安全（§2.3 冻结方案）：LE Secure Connections + 数字比较 + 绑定；
 * IO cap = DISPLAY_YESNO；配对确认经上层 on_numcmp_confirm（KEY 长按接线
 * 属 B2 真机项——本骨架默认拒绝，绝不静默退化 Just Works/公开写入）。
 *
 * 验证状态：仅编译级验证。配对/MTU 协商/大包吞吐/断连半包均为 B0–B5 真机项。
 */
#include "cdt_ble_peripheral.h"

#include <inttypes.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_att.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_sm.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "store/config/ble_store_config.h"

/* 共享传输引擎（P3.4 交付；静态分配，无动态内存） */
#include "cdt_frame.h"
#include "cdt_reassembler.h"

#define TAG "cdt_ble"

/*协商目标：正常连接尽量大 MTU（§2.4）；MTU23 必须可工作（协议正确性不靠大 MTU）*/
#define CDT_BLE_PREFERRED_MTU 517u
/* 单次 ATT 写入上限（ATT_MTU 上限 527−3；NimBLE BLE_ATT_PREFERRED_MTU 缺省域） */
#define CDT_BLE_MAX_ATT_WRITE 524u

/* ------------------------------------------------------------------ */
/* 冻结 UUID（transport.md §2.1；LE 字节序见 cdt_ble_peripheral.h 宏）   */
/* ------------------------------------------------------------------ */

static const ble_uuid128_t u_svc = BLE_UUID128_INIT(CDT_BLE_SVC_UUID_LE);
static const ble_uuid128_t u_rx  = BLE_UUID128_INIT(CDT_BLE_RX_UUID_LE);
static const ble_uuid128_t u_tx  = BLE_UUID128_INIT(CDT_BLE_TX_UUID_LE);

static uint16_t s_rx_val_handle;
static uint16_t s_tx_val_handle;

/* ------------------------------------------------------------------ */
/* 组件状态                                                             */
/* ------------------------------------------------------------------ */

static cdt_ble_cbs_t s_cbs;
static bool s_started;
static volatile uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static volatile uint16_t s_mtu;              /* 协商 ATT MTU（连接内） */
static cdt_ble_stats_t s_stats;

/* 重组引擎静态实例（≈17KiB .bss：16KiB 缓冲 + 512B 位图；P3.4 引擎） */
static cdt_reassembler_t s_rs;
static cdt_frame_header_t s_last_data_hdr;   /* 供齐包后构造 ACK/NACK */
static cdt_apply_result_t s_pending_apply;   /* on_message  wrapper 回填 */

/* ------------------------------------------------------------------ */
/* 前向声明                                                             */
/* ------------------------------------------------------------------ */

static int rx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                        struct ble_gatt_access_ctxt *ctxt, void *arg);
static int tx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                        struct ble_gatt_access_ctxt *ctxt, void *arg);
static int gap_event_cb(struct ble_gap_event *event, void *arg);
static void start_advertising(void);

/* ble_store_config_init 未随公共头导出（IDF bleprph 示例同款前向声明）；
 * 实现位于 NimBLE store/config，绑定密钥持久化到 NVS（§2.3）。 */
void ble_store_config_init(void);

/* ------------------------------------------------------------------ */
/* GATT 服务定义（冻结 UUID + §2.2 权限）                                */
/* ------------------------------------------------------------------ */

static const struct ble_gatt_chr_def s_chrs[] = {
    {
        /* RX：central → 设备，Write / Write Without Response；
         * 加密 + MITM（§2.2）：NimBLE 即 WRITE_ENCRYPTED | WRITE_AUTHEN。 */
        .uuid        = &u_rx.u,
        .access_cb   = rx_access_cb,
        .val_handle  = &s_rx_val_handle,
        .flags       = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP |
                       BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_WRITE_AUTHEN,
    },
    {
        /* TX：设备 → central，Notify/Indicate；特征本身无读/写属性。
         * CCCD 订阅要求加密 + MITM（§2.2）→ 经 READ_ENCRYPTED/AUTHEN 传导。 */
        .uuid        = &u_tx.u,
        .access_cb   = tx_access_cb,
        .val_handle  = &s_tx_val_handle,
        .flags       = BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_INDICATE |
                       BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_READ_AUTHEN,
    },
    { 0 } /* 终结符 */
};

static const struct ble_gatt_svc_def s_svcs[] = {
    {
        .type            = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid            = &u_svc.u,
        .characteristics = s_chrs,
    },
    { 0 } /* 终结符 */
};

/* ------------------------------------------------------------------ */
/* TX 发送（ACK/NACK 经 cdt_build_ack/nack 构造后由此发出）              */
/* ------------------------------------------------------------------ */

static esp_err_t send_notify(const uint8_t *bytes, size_t len)
{
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return ESP_ERR_INVALID_STATE;
    }
    if (bytes == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    uint16_t cap = (uint16_t)(s_mtu ? (s_mtu - 3u) : (23u - 3u));
    if (len > cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    struct os_mbuf *om = ble_hs_mbuf_from_flat(bytes, (uint16_t)len);
    if (om == NULL) {
        s_stats.notify_fail_count++;
        return ESP_ERR_NO_MEM;
    }
    int rc = ble_gatts_notify_custom((uint16_t)s_conn_handle, s_tx_val_handle, om);
    if (rc != 0) {
        s_stats.notify_fail_count++;
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* ------------------------------------------------------------------ */
/* 共享重组引擎回调（组件内部接线；上层只见 on_message 三值契约）        */
/* ------------------------------------------------------------------ */

static void rs_on_complete(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    /* 交付上层（消息→StateStore 组装在 main；transport 不读业务字段），
     * 三值结果驱动 ACK/NACK（transport.md §3.4）。 */
    if (s_cbs.on_message != NULL) {
        s_pending_apply = s_cbs.on_message(s_cbs.user, bytes, len);
    } else {
        s_pending_apply = CDT_APPLY_REJECTED; /* 无上层=不 ACK（防御） */
    }
}

static void rs_on_reject(void *user, cdt_reject_reason_t reason,
                         const cdt_frame_header_t *hdr)
{
    (void)user;
    ESP_LOGW(TAG, "重组拒绝 reason=%d", (int)reason);
    if (hdr != NULL) {
        uint8_t out[CDT_FRAME_SIZE];
        cdt_build_nack(hdr, out); /* 相同头回填、无 payload（§3.2） */
        if (send_notify(out, CDT_FRAME_SIZE) == ESP_OK) {
            s_stats.nack_sent_count++;
        }
    }
}

static const cdt_reassembler_cbs_t s_rs_cbs = {
    .user        = NULL,
    .on_complete = rs_on_complete,
    .on_reject   = rs_on_reject,
};

/* MTU 几何换挡：片容量 = MTU−3−16 变化时必须重建重组上下文
 * （协商发生在业务数据前，§2.4；旧上下文丢弃等价断连清半包语义）。 */
static void reinit_reassembler(uint16_t mtu)
{
    s_mtu = mtu;
    if (mtu >= 19u) {
        (void)cdt_reassembler_init(&s_rs, mtu, &s_rs_cbs);
    }
}

/* ------------------------------------------------------------------ */
/* GATT 访问回调                                                        */
/* ------------------------------------------------------------------ */

static int rx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                        struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return BLE_ATT_ERR_READ_NOT_PERMITTED; /* §2.2：RX 无读属性 */
    }

    uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
    if (len < CDT_FRAME_SIZE) {
        /* 片最小=16B 帧头；短写为越界头，ATT 层报错（§3.3 矛盾头） */
        s_stats.rx_short_write_count++;
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    if (len > CDT_BLE_MAX_ATT_WRITE) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }

    uint8_t tmp[CDT_BLE_MAX_ATT_WRITE];
    if (os_mbuf_copydata(ctxt->om, 0, len, tmp) != 0) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }

    cdt_frame_header_t hdr;
    cdt_frame_decode(tmp, &hdr);
    if (hdr.type != CDT_FRAME_TYPE_DATA) {
        /* RX 特征只收 DATA；ACK/NACK 走 TX 方向（§3.2） */
        return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
    }
    s_stats.rx_fragment_count++;
    s_last_data_hdr = hdr;
    s_pending_apply = CDT_APPLY_REJECTED; /* 缺省：on_complete 未回调则不 ACK */

    int64_t now_ms = esp_timer_get_time() / 1000LL; /* 单调毫秒（引擎虚拟时钟入参） */
    cdt_feed_result_t r = cdt_reassembler_feed(&s_rs, &hdr,
                                               tmp + CDT_FRAME_SIZE,
                                               (size_t)len - CDT_FRAME_SIZE,
                                               now_ms);
    if (r == CDT_FEED_COMPLETED) {
        /* 齐包且 CRC 通过；on_complete 已回填三值结果 → ACK/NACK（§3.4） */
        uint8_t out[CDT_FRAME_SIZE];
        cdt_build_ack_nack(&s_last_data_hdr, s_pending_apply, out);
        if (send_notify(out, CDT_FRAME_SIZE) == ESP_OK) {
            if (s_pending_apply == CDT_APPLY_REJECTED) {
                s_stats.nack_sent_count++;
            } else {
                s_stats.ack_sent_count++; /* applied/duplicate 均 ACK（§3.4） */
            }
        }
    }
    /* 协议层拒绝以 NACK 表达（上）；ATT 层接受写入（Write Without Response
     * 本就无应答，不破坏发送节奏，§2.4） */
    return 0;
}

static int tx_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                        struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;
    /* §2.2：TX 特征本身无读/写属性（CCCD 由栈拦截并受加密+MITM 约束） */
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        return BLE_ATT_ERR_READ_NOT_PERMITTED;
    }
    return BLE_ATT_ERR_WRITE_NOT_PERMITTED;
}

/* ------------------------------------------------------------------ */
/* 广播（Flags + 128bit 服务 UUID 入 adv；设备名 CodexDT 入 scan rsp §2.2）*/
/* ------------------------------------------------------------------ */

static void start_advertising(void)
{
    struct ble_hs_adv_fields f;
    int rc;

    memset(&f, 0, sizeof(f));
    f.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    f.uuids128 = (const ble_uuid128_t *)&u_svc;
    f.num_uuids128 = 1;
    f.uuids128_is_complete = 1;
    rc = ble_gap_adv_set_fields(&f);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv fields 失败 rc=%d", rc);
        return;
    }

    memset(&f, 0, sizeof(f));
    f.name = (const uint8_t *)CDT_BLE_ADV_NAME;
    f.name_len = (uint8_t)strlen(CDT_BLE_ADV_NAME);
    f.name_is_complete = 1;
    rc = ble_gap_adv_rsp_set_fields(&f);
    if (rc != 0) {
        ESP_LOGE(TAG, "scan rsp 失败 rc=%d", rc);
        return;
    }

    struct ble_gap_adv_params ap;
    memset(&ap, 0, sizeof(ap));
    /* 连接参数由 central（macOS）决定，设备不申请极端参数（§2.4） */
    rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                           &ap, gap_event_cb, NULL);
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGE(TAG, "adv start 失败 rc=%d", rc);
    }
}

/* ------------------------------------------------------------------ */
/* GAP 事件                                                             */
/* ------------------------------------------------------------------ */

static int gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
            /* 以连接瞬间的协商 MTU 初始化重组几何（MTU 事件会再次刷新） */
            reinit_reassembler(ble_att_mtu((uint16_t)s_conn_handle));
            ESP_LOGI(TAG, "connected handle=%u mtu=%u",
                     (unsigned)s_conn_handle, (unsigned)s_mtu);
            if (s_cbs.on_link != NULL) {
                s_cbs.on_link(s_cbs.user, true);
            }
        } else {
            start_advertising(); /* 连接失败：恢复广播等待重连 */
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        /* §3.3 接收方 6：断连立刻清空半包与位图；message_id 上下文随之清空 */
        ESP_LOGI(TAG, "disconnect reason=%d；重组上下文清空", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        cdt_reassembler_reset(&s_rs);
        if (s_cbs.on_link != NULL) {
            s_cbs.on_link(s_cbs.user, false);
        }
        start_advertising();
        return 0;

    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "MTU 协商 conn=%u mtu=%u",
                 (unsigned)event->mtu.conn_handle, (unsigned)event->mtu.value);
        reinit_reassembler((uint16_t)event->mtu.value);
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        start_advertising();
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE:
        /* 加密建立后才允许业务读写——GATT 权限由栈按特征标志强制（§2.3） */
        ESP_LOGI(TAG, "enc change status=%d", event->enc_change.status);
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(TAG, "subscribe handle=%u cur_notify=%d cur_indicate=%d",
                 (unsigned)event->subscribe.attr_handle,
                 event->subscribe.cur_notify, event->subscribe.cur_indicate);
        return 0;

    case BLE_GAP_EVENT_PASSKEY_ACTION: {
        struct ble_sm_io pkey;
        memset(&pkey, 0, sizeof(pkey));
        switch (event->passkey.params.action) {
        case BLE_SM_IOACT_NUMCMP:
            /* 数字比较（冻结方案 §2.3）：设备屏显示 6 位 + KEY 长按双向确认。
             * KEY 接线可行性属 B2 真机取证；上层未接确认回调时默认拒绝。 */
            ESP_LOGI(TAG, "数字比较 passkey=%06" PRIu32 "（等待 KEY 确认）",
                     event->passkey.params.numcmp);
            if (s_cbs.on_passkey_display != NULL) {
                s_cbs.on_passkey_display(s_cbs.user,
                                         event->passkey.params.numcmp);
            }
            pkey.action = BLE_SM_IOACT_NUMCMP;
            pkey.numcmp_accept =
                (s_cbs.on_numcmp_confirm != NULL)
                    ? s_cbs.on_numcmp_confirm(s_cbs.user,
                                              event->passkey.params.numcmp)
                    : false;
            return ble_sm_inject_io(event->passkey.conn_handle, &pkey);
        case BLE_SM_IOACT_NONE:
            pkey.action = BLE_SM_IOACT_NONE;
            return ble_sm_inject_io(event->passkey.conn_handle, &pkey);
        default:
            /* 不支持静态 passkey/OOB：冻结方案仅数字比较（不静默降级） */
            ESP_LOGW(TAG, "配对动作 %d 不在冻结方案内，拒绝",
                     event->passkey.params.action);
            pkey.action = BLE_SM_IOACT_NONE;
            return ble_sm_inject_io(event->passkey.conn_handle, &pkey);
        }
    }

    default:
        return 0;
    }
}

/* ------------------------------------------------------------------ */
/* NimBLE 宿主                                                          */
/* ------------------------------------------------------------------ */

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure addr 失败 rc=%d", rc);
        return;
    }
    start_advertising(); /* 宿主同步完成 → 开始广播（Bridge 按 UUID 过滤） */
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "NimBLE 宿主复位 reason=%d", reason);
}

static void host_task(void *param)
{
    (void)param;
    nimble_port_run(); /* 返回于 nimble_port_stop() */
    nimble_port_freertos_deinit();
}

/* ------------------------------------------------------------------ */
/* 生命周期 API                                                          */
/* ------------------------------------------------------------------ */

esp_err_t cdt_ble_periph_start(const cdt_ble_cbs_t *cbs)
{
    if (cbs == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }

    s_cbs = *cbs;
    memset(&s_stats, 0, sizeof(s_stats));
    s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
    s_mtu = 0;

    int rc = nimble_port_init();
    if (rc != 0) {
        ESP_LOGE(TAG, "nimble_port_init 失败 rc=%d", rc);
        return ESP_FAIL;
    }

    /* 配对：LE Secure Connections + MITM + 绑定 + 数字比较（§2.3 冻结） */
    ble_hs_cfg.sync_cb          = on_sync;
    ble_hs_cfg.reset_cb         = on_reset;
    ble_hs_cfg.sm_bonding       = 1;
    ble_hs_cfg.sm_mitm          = 1;
    ble_hs_cfg.sm_sc            = 1;                     /* SC（ECDH P-256） */
    ble_hs_cfg.sm_io_cap        = BLE_HS_IO_DISPLAY_YESNO; /* 数字比较 */
    ble_hs_cfg.sm_our_key_dist  = BLE_SM_PAIR_KEY_DIST_ENC;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC;

    ble_att_set_preferred_mtu((uint16_t)CDT_BLE_PREFERRED_MTU);

    ble_svc_gap_init();
    ble_svc_gatt_init();

    rc = ble_gatts_count_cfg(s_svcs);
    if (rc == 0) {
        rc = ble_gatts_add_svcs(s_svcs);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "GATT 服务注册失败 rc=%d", rc);
        nimble_port_deinit();
        return ESP_FAIL;
    }

    rc = ble_svc_gap_device_name_set(CDT_BLE_ADV_NAME);
    if (rc != 0) {
        ESP_LOGE(TAG, "设备名设置失败 rc=%d", rc);
        nimble_port_deinit();
        return ESP_FAIL;
    }

    ble_store_config_init(); /* LTK 绑定存 NVS（§2.3：绑定后重连免配对） */

    nimble_port_freertos_init(host_task);
    s_started = true;
    ESP_LOGI(TAG, "BLE peripheral 骨架启动 svc=" CDT_BLE_SVC_UUID_STR);
    return ESP_OK;
}

esp_err_t cdt_ble_periph_stop(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    int rc = nimble_port_stop();
    if (rc == 0) {
        nimble_port_deinit();
    } else {
        ESP_LOGW(TAG, "nimble_port_stop rc=%d", rc);
        return ESP_FAIL;
    }
    s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
    s_started = false;
    return ESP_OK;
}

esp_err_t cdt_ble_periph_notify(const uint8_t *bytes, size_t len)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    return send_notify(bytes, len);
}

uint16_t cdt_ble_periph_current_mtu(void)
{
    return s_conn_handle == BLE_HS_CONN_HANDLE_NONE ? 0u : s_mtu;
}

bool cdt_ble_periph_poll_reassembly(int64_t now_ms)
{
    if (!s_started) {
        return false;
    }
    return cdt_reassembler_poll(&s_rs, now_ms);
}

cdt_ble_stats_t cdt_ble_get_stats(void)
{
    return s_stats;
}
