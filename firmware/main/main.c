/*
 * main.c — 整机产品形态固件（整机集成 v1，A3+A4 合并，含烧录与真机联调）
 *
 * 任务书（产品时刻）：设备经 Wi-Fi 连 Mac Bridge（WSS），实时显示 mock 生命周期
 * 状态（working→needs_you→done），KEY 翻页，电压上屏。
 *
 * 启动序列（DEVELOPMENT_PLAN §7.3 BOOT_CHECK 语义）：
 *   显示/初始化 → battery init → 首批采样 → Power FSM BOOT_CHECK
 *   （健康电压 → ALLOW_RADIO_START）→ WiFi STA（dev_net）→ WSS（cdt_wss_client）
 *   → on_text（R4 收件槽：capacity-1 普通槽 + capacity-1 优先提醒槽，
 *   互斥短临界区交接、消费者独享缓冲解析）→ StateStore（单解析任务语义）→
 *   presenter（Runtime：battery FSM 状态 / link_state / selected_page / 静音）
 *   → ui_apply。
 *
 * 主循环（10ms tick）：
 *   - lv_timer_handler（LVGL 时基 = LV_TICK_CUSTOM esp_timer）；
 *   - cdt_key 事件队列（短按翻页 / 长按静音，P4.5 真实按键顺带验证）；
 *   - battery 批采样（10s 常规 / 1Hz 近阈值，§7.1；样本喂 shared/power FSM）；
 *   - link 新鲜度（45s stale / 150s disconnected，INTERFACES §4）；
 *   - 1Hz 或事件驱动重渲染（同步整屏 flush）。
 *
 * LOW BATTERY 强制页：FSM CRITICAL/SLEEP_PREP → presenter 抢占为 LOW BATTERY 页
 * → cdt_power_exec §7.3 停止顺序 → Deep Sleep。**逻辑已接线；真机低压触发验证
 * 归 P5**（真触发需要电压 ≤3.6V 持续 30s——本任务不人为制造低压，不做任何
 * "低压已实测"声明；shared/power FSM 行为已由 PC 端 P5.1 虚拟时钟断言覆盖）。
 *
 * 凭据（WIFI SSID/PASS、Bridge 地址、token、CA、SPKI pin）经
 * firmware/main/dev_net_config.h 注入；该文件已 gitignore，绝不入库；
 * 缺失时本文件 #error（离线演示构建用 CDT_DEVNET_OFFLINE，不编入任何凭据，
 * 显示 DISCONNECTED 页）。串口日志：连接状态、每快照 seq/CRC、电压、页面切换；
 * token 只以 SHA-256 指纹前 8 hex 出现（cdt_wss_client 保证）。
 */
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_attr.h"
#include "nvs_flash.h"

/* ---- 联调凭据注入（gitignore 文件；缺失即编译错误，绝不给默认值）---- */
#if defined(CDT_DEVNET_OFFLINE)
#define CDT_HAS_NET_CONFIG 0
/* 离线演示构建：不编入任何网络凭据（红线），链路如实显示 DISCONNECTED */
#elif defined(__has_include)
#if __has_include("dev_net_config.h")
#include "dev_net_config.h"
#define CDT_HAS_NET_CONFIG 1
#else
#error "缺少 firmware/main/dev_net_config.h：cp firmware/main/dev_net_config.h.template " \
       "firmware/main/dev_net_config.h 并按模板注释填入凭据（该文件已 gitignore）。" \
       "无网演示构建可用：idf.py -C firmware build -DCDT_DEVNET_OFFLINE=1"
#endif
#else
#include "dev_net_config.h"
#define CDT_HAS_NET_CONFIG 1
#endif

#include "app_inbox.h"        /* R4：收件槽跨任务所有权 + 优先提醒槽 */
#include "app_power_idle.h"   /* ZC9：Wi-Fi 空闲动态降档纯决策（宿主测试单源） */
#include "app_power_policy.h" /* R3a/R3b：电源动作分发策略（宿主测试单源） */
#include "cdt_battery.h"
#include "cdt_key.h"
#include "cdt_nav.h"
#include "cdt_parser.h"
#include "cdt_power.h"
#include "cdt_power_exec.h"
#include "cdt_presenter.h"
#include "cdt_runtime.h"
#include "cdt_store.h"
#include "cdt_ui.h"
#include "cdt_view.h"
#include "cdt_wss_client.h"
#include "codex_state.h"
#include "dev_net.h"
#include "lvgl_port.h"
#include "st7305.h"
#include "transport_sel.h" /* ZC11：传输选择（Kconfig 编译期 WiFi/WSS 或 BLE） */

/* CRC32：shared/transport/cdt_frame.h:91 冻结签名（cdt_crc32）。该头与
 * shared/display/cdt_frame.h 同名同 guard（CDT_FRAME_H）无法同 TU 包含，
 * 按冻结签名本地声明；实现来自 components/transport（同 archive 内
 * cdt_crc32.o，ble_peripheral/wss 已引用，无重复符号）。 */
uint32_t cdt_crc32(const uint8_t *data, size_t len);

#define TAG "cdt_app"

/* 整机集成（真机 1301）实测：内部 RAM 仅 ~113KB，app 栈从 28KB 缩到 12KB——
 * 大对象（store/收件槽）已在 PSRAM 静态区，本任务无 KB 级栈局部量；
 * 释放的内部 RAM 给 WSS client 任务栈（TLS 握手必需，见 wss_client.c）与
 * esp-aes DMA 弹跳缓冲（真机曾因内部内存耗尽报 "esp-aes: Failed to allocate
 * memory"，见 artifacts/board/integration/serial_integration*.log）。 */
#define APP_TASK_STACK_BYTES (12 * 1024)
#define APP_TICK_MS 10
#define RENDER_PERIOD_MS 1000       /* 1Hz 重渲染（电压/时长推进；§6 每 10–30s 起步，1s 上限保守） */
#define BATTERY_PERIOD_NORMAL_MS 10000 /* §7.1 常规 10s */
#define BATTERY_PERIOD_FAST_MS 1000    /* §7.1 近阈值 1Hz */
#define LINK_STALE_MS 45000u           /* INTERFACES §4 */
#define LINK_DEAD_MS 150000u           /* INTERFACES §4：disconnected 并重连 */
#define WIFI_WAIT_IP_MS 15000          /* WiFi 取 IP 后再起 WSS（超时也起，靠退避） */

/* ZC9 空闲动态降档编译门（P5.2 后半）：仅在 MIN_MODEM 基线（产品默认
 * CDT_WIFI_PS_MODE=1）启用。NONE/MAX 显式配置视为「钉死档位」的运维选择，
 * 动态降档不覆盖；离线演示构建无无线，不参与。 */
#if CDT_HAS_NET_CONFIG && defined(CONFIG_CDT_WIFI_PS_MODE) && CONFIG_CDT_WIFI_PS_MODE == 1
#define APP_IDLE_DOWNGRADE_ENABLED 1
#else
#define APP_IDLE_DOWNGRADE_ENABLED 0
#endif

/* ------------------------------------------------------------------ */
/* 大对象静态化（不占任务栈；store 内含 2×17KiB scratch）。store ~34KiB + */
/* 收件槽三缓冲 3×16KiB（普通/优先/消费者独享，R4）→ PSRAM 静态区         */
/* （CONFIG_SPIRAM_ALLOW_BSS_IN_PSRAM；LVGL 绘制缓冲仍留内部 DRAM）。     */
/* ------------------------------------------------------------------ */
EXT_RAM_BSS_ATTR static cdt_state_store_t s_store;   /* AppState 整包替换（单解析任务=本任务） */
static cdt_runtime_t s_runtime;
static cdt_view_t s_view;
static cdt_nav_t s_nav;
static cdt_power_fsm_t s_fsm;

/* capacity-1 普通槽 + capacity-1 优先提醒槽 + 消费者独享解析缓冲（R4）。
 * 所有权与锁语义见 app_inbox.h；本文件只持有消费者缓冲——app_inbox_take 在
 * 短临界区内把槽内容复制进来，CRC/JSON 解析在锁外对本缓冲进行（审核 R4）。 */
EXT_RAM_BSS_ATTR static uint8_t s_inbox_cons[APP_INBOX_MSG_MAX];

/* WSS 链路镜像（on_link 在 wss 任务上下文，只写 volatile + 日志） */
static volatile cdt_wss_link_state_t s_wss_state = CDT_WSS_LINK_DISCONNECTED;
static volatile cdt_wss_failure_t s_wss_fail = CDT_WSS_FAIL_NONE;
static volatile uint32_t s_wss_connected_ms; /* 进入 CONNECTED 的单调 ms（0=未连接）；R4 新鲜度重同步用 */

/* battery / link 状态 */
static cdt_power_sample_t s_last_sample;
static bool s_have_sample;
static bool s_have_rx;
static uint32_t s_last_rx_ms;   /* 单调 ms；收到合法新 seq 快照时刷新（§4） */
static uint32_t s_applied_count;
#if APP_IDLE_DOWNGRADE_ENABLED
static uint32_t s_last_key_ms;        /* ZC9：最近按键单调 ms（0=无；handle_key 刷新） */
static bool s_idle_ps_max_applied;    /* ZC9：已应用到 MAX（调用侧镜像；dev_net 内还有档位去重） */
static int64_t s_idle_max_entered_ms; /* ZC9：最近一次切 MAX 的单调 ms（0=从未；回 MIN 后保留供迟滞） */
#endif

/* 静音（ACK=本地静音/已读，绝不等于批准 Codex 操作）：记录静音时选中线程
 * 的 turn_id；新 turn（不同 turn_id 且有新 pending）可再次提醒（§6）。 */
static bool s_muted;
static char s_muted_turn[CDT_MAX_ID_BYTES + 1];

static QueueHandle_t s_key_queue;
static bool s_radio_started;

/* R3a：睡眠请求锁存——power_handle_actions 置位，调用点检查后执行
 * do_sleep_sequence（真机不返回）。启动（BOOT_CHECK）与运行时共用同一条
 * 「FSM 动作 → 锁存 → 执行」路径，一次性局部 bool 不再丢失睡眠请求。 */
static volatile bool s_sleep_latched;

/* ------------------------------------------------------------------ */
/* 墙钟助手                                                              */
/* ------------------------------------------------------------------ */
static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000LL;
}

/* ------------------------------------------------------------------ */
/* 回调：KEY（esp_timer 任务，只入队）与 WSS（wss 任务，只写槽/镜像）      */
/* 注：驱动事件类型是 cdt_key_hw_event_t（input 组件；与 UI 层             */
/* cdt_nav.h 的 cdt_key_event_t 同名冲突故更名），在 handle_key 映射。     */
/* ------------------------------------------------------------------ */
static void key_cb(void *user, cdt_key_hw_event_t ev)
{
    (void)user;
    if (s_key_queue != NULL) {
        xQueueSend(s_key_queue, &ev, 0); /* 满则丢，UI 主循环节奏远快于按键 */
    }
}

static void wss_on_text(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    /* R4：wss 任务上下文只做「互斥短临界区入槽」（app_inbox_produce 内完成
     * 优先判定+复制+计数），不再与主任务无锁共享同一缓冲。 */
    app_inbox_produce(bytes, len);
}

/* WSS 链路状态名（日志用；cdt_wss_client.h 未提供，本地映射） */
static const char *wss_state_name(cdt_wss_link_state_t st)
{
    switch (st) {
    case CDT_WSS_LINK_DISCONNECTED: return "disconnected";
    case CDT_WSS_LINK_CONNECTING:   return "connecting";
    case CDT_WSS_LINK_CONNECTED:    return "connected";
    case CDT_WSS_LINK_BACKOFF_WAIT: return "backoff";
    case CDT_WSS_LINK_CONFIG_ERROR: return "config_error";
    default:                        return "?";
    }
}

static void wss_on_link(void *user, cdt_wss_link_state_t st,
                        cdt_wss_failure_t f, int detail)
{
    (void)user;
    s_wss_state = st;
    s_wss_fail = f;
    if (st == CDT_WSS_LINK_CONNECTED) {
        s_wss_connected_ms = (uint32_t)(esp_timer_get_time() / 1000LL);
    } else {
        s_wss_connected_ms = 0; /* 离开连接态：重同步观察窗重新起算 */
    }
    if (f == CDT_WSS_FAIL_NONE) {
        ESP_LOGI(TAG, "[wss] link=%s detail=%d", wss_state_name(st), detail);
    } else {
        ESP_LOGW(TAG, "[wss] link=%s fail=%s detail=%d",
                 wss_state_name(st), cdt_wss_failure_name(f), detail);
    }
}

/* ------------------------------------------------------------------ */
/* Runtime 合并 + presenter + 渲染                                       */
/* ------------------------------------------------------------------ */
static cdt_link_state_t compute_link_state(int64_t now)
{
    if (!s_have_rx) {
        return CDT_LINK_DISCONNECTED; /* 尚无任何合法快照：如实显示断开 */
    }
    /* ZC11：链路状态按编译期选定的传输判定——BLE 看 GAP 连接镜像，
     * WIFI 看 WSS 状态机（原逻辑零变化）。 */
    if (cdt_transport_sel_is_ble() ? !cdt_transport_sel_ble_link_up()
                                   : (s_wss_state != CDT_WSS_LINK_CONNECTED)) {
        return CDT_LINK_DISCONNECTED; /* transport 层已断（退避/配置错误） */
    }
    uint32_t since = (uint32_t)(now - (int64_t)s_last_rx_ms);
    if (since < LINK_STALE_MS) {
        return CDT_LINK_CONNECTED;
    }
    if (since < LINK_DEAD_MS) {
        return CDT_LINK_STALE;
    }
    return CDT_LINK_DISCONNECTED;
}

static void runtime_update(int64_t now)
{
    if (s_have_sample && s_last_sample.valid) {
        s_runtime.battery_valid = true;
        s_runtime.battery_mv = s_last_sample.battery_mv;
        int32_t pct = ((int32_t)s_last_sample.battery_mv - 3600) * 100 / 600;
        if (pct < 0) {
            pct = 0;
        }
        if (pct > 100) {
            pct = 100;
        }
        s_runtime.usable_percent = (uint8_t)pct; /* §7.1 单调线性估算 */
    } else {
        s_runtime.battery_valid = false;
        s_runtime.battery_mv = 0;
        s_runtime.usable_percent = 0;
    }
    s_runtime.charging = CDT_PRESENCE_UNKNOWN;      /* 本板无充电检测（HARDWARE §1.3） */
    s_runtime.external_power = CDT_PRESENCE_UNKNOWN; /* 不根据高电压猜充电 */
    s_runtime.power_state = s_fsm.state;
    snprintf(s_runtime.transport, sizeof s_runtime.transport, "%s",
             cdt_transport_sel_name()); /* "wifi"/"ble"（§1a 枚举，编译期选定） */
    s_runtime.link_state = compute_link_state(now);
    s_runtime.last_rx_monotonic_ms = s_last_rx_ms;
    s_runtime.selected_page = s_nav.page;
    s_runtime.muted_attention_present = s_muted;
    s_runtime.tz_offset_min = (int16_t)CONFIG_CDT_TZ_OFFSET_MIN; /* 顶栏时钟显示换算 */
}

static cdt_page_t s_logged_page; /* 页面切换事件日志 */

static void render(int64_t now, const char *why)
{
    runtime_update(now);
    const cdt_app_state_t *snap = cdt_state_store_snapshot(&s_store);
    cdt_present(snap, &s_runtime, (uint32_t)now, &s_view);
    cdt_ui_apply_nav(&s_view, &s_nav);
    cdt_lvgl_port_refresh(); /* 同步整屏 flush 落面板 */

    if (s_view.page != s_logged_page) {
        ESP_LOGI(TAG, "[page] %d -> %d (%s) forced=%d lowbat=%d",
                 (int)s_logged_page, (int)s_view.page, why,
                 (int)s_view.low_battery_forced, (int)s_view.battery_valid);
        s_logged_page = s_view.page;
    }
}

/* ------------------------------------------------------------------ */
/* KEY 事件消费（短按翻页 / 长按静音；§6）                                */
/* ------------------------------------------------------------------ */
static void handle_key(cdt_key_hw_event_t ev, int64_t now, bool *need_render)
{
    if (ev != CDT_KEY_EVENT_KEY_SHORT && ev != CDT_KEY_EVENT_KEY_LONG) {
        return; /* BOOT 键保留下载用途（§1），不接业务 */
    }
#if APP_IDLE_DOWNGRADE_ENABLED
    s_last_key_ms = (uint32_t)now; /* ZC9：真实按键活动 = 空闲降档的立即回 MIN 源 */
#endif
    cdt_key_event_t key = ev == CDT_KEY_EVENT_KEY_SHORT ? CDT_KEY_SHORT_PRESS
                                                        : CDT_KEY_LONG_PRESS;
    uint32_t acts = cdt_nav_key(&s_nav, &s_view, key);
    if (acts & CDT_NAV_ACT_PAGE) {
        s_runtime.selected_page = s_nav.page;
        ESP_LOGI(TAG, "[key] 短按 -> page=%d", (int)s_nav.page);
        *need_render = true;
    }
    if (acts & CDT_NAV_ACT_MUTE) {
        /* 长按：静音当前提醒（只 ACK 本地已读；绝不等于批准 Codex 操作） */
        const cdt_app_state_t *snap = cdt_state_store_snapshot(&s_store);
        s_muted = true;
        s_muted_turn[0] = '\0';
        if (snap != NULL && snap->selected_thread_id_present) {
            for (int i = 0; i < snap->thread_count; i++) {
                const cdt_thread_t *t = &snap->threads[i];
                if (strcmp(t->id, snap->selected_thread_id) == 0) {
                    if (t->turn_id_present) {
                        snprintf(s_muted_turn, sizeof s_muted_turn, "%s", t->turn_id);
                    }
                    break;
                }
            }
        }
        ESP_LOGI(TAG, "[key] 长按 -> mute 当前提醒（turn=%s）",
                 s_muted_turn[0] ? s_muted_turn : "-");
        *need_render = true;
    }
}

/* 新 turn 的新 pending 可再次提醒（§6）：新快照选中线程 turn_id 变化且有
 * pending → 解除静音。 */
static void unmute_if_new_reminder(const cdt_app_state_t *snap)
{
    if (!s_muted || snap == NULL || !snap->selected_thread_id_present) {
        return;
    }
    for (int i = 0; i < snap->thread_count; i++) {
        const cdt_thread_t *t = &snap->threads[i];
        if (strcmp(t->id, snap->selected_thread_id) != 0) {
            continue;
        }
        if (t->attention_present && t->attention.pending_count > 0 &&
            t->turn_id_present && s_muted_turn[0] != '\0' &&
            strcmp(t->turn_id, s_muted_turn) != 0) {
            s_muted = false;
            ESP_LOGI(TAG, "[mute] 新 turn 新提醒 -> 解除静音");
        }
        break;
    }
}

/* ------------------------------------------------------------------ */
/* 快照应用（唯一解析任务：StateStore 整包替换）                          */
/* R4：每轮只取一条（优先槽先出，见 app_inbox_take）；取件=短临界区复制到   */
/* 消费者独享缓冲 s_inbox_cons，CRC/JSON 解析在锁外进行，解析期间不持锁、   */
/* 不关中断。两条消息天然各得一次渲染窗口（提醒先于后续终态上屏）。         */
/* ------------------------------------------------------------------ */
static void handle_inbox(int64_t now, bool *need_render)
{
    size_t len = 0;
    if (!app_inbox_take(s_inbox_cons, sizeof s_inbox_cons, &len)) {
        return;
    }
    uint32_t crc = cdt_crc32(s_inbox_cons, len);
    cdt_parse_result_t r = cdt_state_store_apply(&s_store, s_inbox_cons, len);
    if (r == CDT_PARSE_OK) {
        s_have_rx = true;
        s_last_rx_ms = (uint32_t)now; /* §4：只在合法新 seq 快照时刷新 last_rx */
        s_applied_count++;
        const cdt_app_state_t *cur = cdt_state_store_snapshot(&s_store);
        int pending0 = cur->thread_count && cur->threads[0].attention_present
                           ? (int)cur->threads[0].attention.pending_count : -1;
        ESP_LOGI(TAG,
                 "[state] applied #%lu seq=%llu epoch=%s bytes=%u crc32=0x%08"
                 PRIx32 " src=%d stale=%d threads=%u pending0=%d"
                 " dropped=%lu dropped_prio=%lu",
                 (unsigned long)s_applied_count,
                 (unsigned long long)cur->seq,
                 cur->bridge_epoch, (unsigned)len, crc,
                 (int)cur->source.kind, (int)cur->source.stale,
                 (unsigned)cur->thread_count, pending0,
                 (unsigned long)app_inbox_dropped_normal(),
                 (unsigned long)app_inbox_dropped_priority());
        cdt_nav_clamp(&s_nav, &s_view); /* 内容收缩后子页回钳 */
        unmute_if_new_reminder(cur);
        *need_render = true;
    } else if (r == CDT_PARSE_IGNORED_STALE_SEQ) {
        ESP_LOGW(TAG, "[state] ignored seq（同 epoch 重复/回卷）bytes=%u crc32=0x%08" PRIx32,
                 (unsigned)len, crc);
    } else {
        ESP_LOGE(TAG, "[state] REJECTED code=%d bytes=%u crc32=0x%08" PRIx32 "（保留旧快照）",
                 (int)r, (unsigned)len, crc);
    }
}

/* ------------------------------------------------------------------ */
/* Power FSM 动作执行（含 LOW BATTERY 强制页 + §7.3 停止顺序；触发需真实   */
/* 电压 ≤3.6V，本任务不人为制造——逻辑接线完整，真机低压验证归 P5）        */
/* R3a：本函数是启动（BOOT_CHECK）与运行时共用的唯一睡眠执行路径；睡眠请求 */
/* 经 s_sleep_latched 锁存后到达这里，不依赖一次性的局部 bool。            */
/* ------------------------------------------------------------------ */
static void cb_stop_radio(void *user)
{
    (void)user;
    cdt_transport_sel_radio_stop(); /* ZC11：BLE 模式停外设；WIFI 模式 no-op（下行照旧） */
    esp_err_t r1 = cdt_wss_stop();
    esp_err_t r2 = dev_net_stop();
    ESP_LOGW(TAG, "[lowbat] radio stop: wss=%s net=%s", esp_err_to_name(r1),
             esp_err_to_name(r2));
}

static void cb_pa_off(void *user)
{
    (void)user;
    /* §7.3 步④/⑥：PA=GPIO46 拉低（关功放，避免深睡反向供电；HARDWARE §1.5） */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << CDT_POWER_EXEC_PA_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    gpio_set_level(CDT_POWER_EXEC_PA_GPIO, 0);
}

static void cb_lcd_final(void *user, cdt_power_exec_lcd_mode_t mode)
{
    (void)user;
    /* ST7305 为反射 LCD：HOLD 模式面板自保持末帧（LOW BATTERY 页），无需命令；
     * OFF 模式（保持功耗超标时的保护路径）与保持/关闭对照实测归 P5.4。 */
    ESP_LOGW(TAG, "[lowbat] lcd_final mode=%d（HOLD=面板自保持；对照实测归 P5.4）", (int)mode);
}

static bool cb_poll_flush(void *user)
{
    (void)user;
    return true; /* 本固件 flush 为同步（cdt_lvgl_port_refresh 返回即落面板） */
}

static void cb_wait_ms(void *user, uint32_t ms)
{
    (void)user;
    vTaskDelay(pdMS_TO_TICKS(ms));
}

static int64_t cb_now(void *user)
{
    (void)user;
    return now_ms();
}

static void do_sleep_sequence(cdt_power_action_t acts)
{
    /* 末帧：LOW BATTERY 强制页已由 presenter 抢占（power_state=CRITICAL/
     * SLEEP_PREP → view.low_battery_forced），同步刷新落面板后即末帧完成。 */
    render(now_ms(), "lowbat-final-frame");

    cdt_power_exec_flow_reset(CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);
    cdt_power_exec_prepare_ops_t prep = { .user = NULL, .cancel_reconnect = cb_stop_radio };
    cdt_power_exec_prepare(acts, &prep);

    cdt_power_exec_wait_ops_t wait = {
        .user = NULL, .poll_flush = cb_poll_flush,
        .wait_ms = cb_wait_ms, .now_ms = cb_now,
    };
    bool flushed = cdt_power_exec_final_frame(&wait, CDT_POWER_EXEC_FINAL_FRAME_TIMEOUT_MS);
    ESP_LOGW(TAG, "[lowbat] 末帧 flushed=%d（超时/放弃也要睡，§7.2）", flushed);

    cdt_power_exec_sleep_ops_t sleep_ops = {
        .user = NULL,
        .stop_transport_radio = cb_stop_radio,
        .power_off_periph = cb_pa_off,
        .lcd_final = cb_lcd_final,
    };
    cdt_power_exec_sleep_config_t cfg = {
        .lcd_mode = CDT_POWER_EXEC_LCD_HOLD,
        .key_deep_wake = false, /* KEY 深睡唤醒 unverified（HARDWARE §4.2）；保底 PWR 上电 */
        .low_battery_reason = true,
    };
    ESP_LOGE(TAG, "[lowbat] 进入 §7.3 停止顺序 → Deep Sleep（唤醒保底 = PWR 重新上电）");
    cdt_power_exec_sleep(&cfg, &sleep_ops, cdt_power_exec_rtc_reason_io());
    /* 真机不返回 */
}

static void power_handle_actions(cdt_power_action_t acts, int64_t now, bool *need_render)
{
    if (acts == CDT_POWER_ACT_NONE) {
        return;
    }
    char buf[96];
    ESP_LOGI(TAG, "[power] %s -> %s", cdt_power_state_str(s_fsm.state),
             cdt_power_actions_str(buf, sizeof buf, acts));

    /* R3a：睡眠请求锁存至执行（掩码单源见 app_power_policy.h；含
     * DEEP_SLEEP_READY——审核指出旧 dispatcher 不处理该动作）。启动与运行时
     * 共用本路径；调用点（boot_check/主循环）检查 s_sleep_latched 后调
     * do_sleep_sequence，一次性局部 bool 不再丢失请求。 */
    if (app_power_sleep_requested(acts)) {
        s_sleep_latched = true;
    }
    if (acts & CDT_POWER_ACT_BATTERY_FAULT) {
        ESP_LOGE(TAG, "[power] BATTERY_FAULT（连续 3 次无效样本）：关高功耗活动并提示");
#if CDT_HAS_NET_CONFIG
        /* 裁决1（app_power_policy.h 单源谓词）：§7.1 字面「关闭高功耗活动」——
         * 故障成立当拍立即停 WSS（快照流/TLS 心跳即停）；WiFi STA 的最终关闭
         * 仍归既有宽限→受控休眠路径（do_sleep_sequence → cb_stop_radio 一次
         * 关 wss+net），两段不重复建模。恢复样本有效后不自动重启无线（v1 保守
         * 策略）：与 KEY 深睡唤醒同款——FSM 故障恢复只发 BATTERY_FAULT_
         * RECOVERED（不发 ALLOW_RADIO_START），重启保底 = PWR 重新上电走
         * 冷启动 BOOT_CHECK 重新判定；故障期内的采样有效性不足以背书无线
         * 重启决策，不猜。cdt_wss_stop 阻塞 ≤6s（组件兜底），故障路径可接受
         * （10s 宽限内必入睡）。 */
        if (app_power_fault_stop_radio_now(acts) && s_radio_started) {
            esp_err_t fr = cdt_wss_stop();
            ESP_LOGW(TAG, "[power] BATTERY_FAULT 即停无线: wss=%s（恢复后不自动重启，"
                     "保底 PWR 重新上电）", esp_err_to_name(fr));
        }
#endif
        *need_render = true;
    }
    if (acts & (CDT_POWER_ACT_ENTER_LOW_WARN | CDT_POWER_ACT_EXIT_LOW_WARN |
                CDT_POWER_ACT_BATTERY_FAULT_RECOVERED | CDT_POWER_ACT_SAMPLE_GAP_RESET)) {
        *need_render = true;
    }
    if (acts & CDT_POWER_ACT_ALLOW_RADIO_START && !s_radio_started) {
        ESP_LOGI(TAG, "[power] BOOT_CHECK 允许无线");
    }
    if (acts & CDT_POWER_ACT_BLOCK_RADIO_START) {
        ESP_LOGE(TAG, "[power] BOOT_CHECK 拒绝无线（低压唤醒门禁）");
    }
}

/* ------------------------------------------------------------------ */
/* BOOT_CHECK：battery init → 首批采样 → FSM 启动判定（先 ADC 后无线）    */
/* ------------------------------------------------------------------ */
static bool boot_check(void)
{
    cdt_power_params_t params;
    cdt_power_params_init(&params); /* §7.2 初值 + 校准恒等（P4.4 定标后回填） */

    cdt_battery_config_t bcfg = {
        .batch_n = 9,               /* §7.1：每批 9 次取中位 */
        .divider_permille = 3000,   /* ×3 分压（HARDWARE §2，待表计核验） */
        .params = params,           /* FSM 同源参数（契约） */
    };
    esp_err_t err = cdt_battery_init(&bcfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cdt_battery_init 失败: %s", esp_err_to_name(err));
    }

    /* RTC 低压原因标记（深睡唤醒门禁；冷启动自然无效） */
    bool low_wake_hint = false;
    bool rtc_allow = cdt_power_exec_boot_gate_allow_radio(cdt_power_exec_rtc_reason_io(),
                                                          &low_wake_hint);
    cdt_power_init(&s_fsm, &params, low_wake_hint);
    ESP_LOGI(TAG, "[power] BOOT_CHECK：rtc_gate_allow=%d low_wake_hint=%d",
             rtc_allow, low_wake_hint);
    if (!rtc_allow) {
        /* 低压标记有效：拒无线（§7.3 BOOT_CHECK 行），FSM 走 recovery 判定 */
        ESP_LOGE(TAG, "[power] RTC 低压标记有效 → 拒绝启动无线（保底 PWR 上电重启后依样本判定）");
    }

    /* 首批采样 → FSM 启动判定 */
    int64_t now = now_ms();
    cdt_power_sample_t sample;
    err = cdt_battery_sample_batch(now, &sample);
    if (err == ESP_OK) {
        s_last_sample = sample;
        s_have_sample = true;
        ESP_LOGI(TAG, "[battery] 首批 mv=%u valid=%d", sample.battery_mv, sample.valid);
    } else {
        /* R3b：首批失败同样以「当前时间、valid=false」进 FSM（审核：不得把
         * 旧样本/零值当有效时间戳复用），故障策略由此推进。 */
        s_last_sample = app_power_invalid_sample(now);
        s_have_sample = true;
        ESP_LOGE(TAG, "[battery] 首批采样失败: %s（invalid 样本喂 FSM 故障策略）",
                 esp_err_to_name(err));
    }
    cdt_power_input_t in = {
        .kind = CDT_POWER_IN_SAMPLE,
        .sample = s_last_sample,
    };
    cdt_power_action_t acts = cdt_power_step(&s_fsm, &in, now);
    bool need_render = false;
    power_handle_actions(acts, now, &need_render);

    /* R3a：BOOT_CHECK 收到睡眠动作（低压唤醒 recovery 不满足 → SLEEP_PREP，
     * DEVELOPMENT_PLAN §7.3 BOOT_CHECK 行）必须真实执行——审核指出旧代码只把
     * 动作用于计算允许无线的 bool，一次性动作被丢弃，设备继续主循环不睡。
     * 显示已初始化（app_task 先 render "boot"），do_sleep_sequence 落末帧后
     * 深睡，真机不返回。 */
    if (s_sleep_latched) {
        do_sleep_sequence(acts);
    }

    bool healthy = (acts & CDT_POWER_ACT_ALLOW_RADIO_START) != 0 && !s_sleep_latched;
    ESP_LOGI(TAG, "[power] BOOT_CHECK 判定 -> %s（电压 %s）",
             healthy ? "允许无线" : "受限/拒无线",
             (s_have_sample && s_last_sample.valid) ? "有效" : "无效");
    return healthy && rtc_allow;
}

/* ------------------------------------------------------------------ */
/* 无线启动：WiFi STA → 等 IP → WSS（冻结退避由 cdt_wss_client 驱动）     */
/* ------------------------------------------------------------------ */
#if CDT_HAS_NET_CONFIG
/* R4 新鲜度重同步用的 WSS 配置副本（浅拷贝；uri/pin/ca/token 均为静态存储，
 * 生命周期覆盖 stop→start）。 */
static cdt_wss_config_t s_wss_cfg;
static uint32_t s_last_resync_ms; /* 上次重同步单调 ms（节流 = LINK_DEAD_MS） */

static void hex_to_pin(const char *hex, uint8_t out[32])
{
    memset(out, 0, 32);
    for (int i = 0; i < 32; i++) {
        unsigned v = 0;
        char c0 = hex[2 * i], c1 = hex[2 * i + 1];
        if (c0 == '\0' || c1 == '\0') {
            return;
        }
        sscanf(&hex[2 * i], "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

static void start_radio(void)
{
    esp_err_t err = dev_net_start(DEV_WIFI_SSID, DEV_WIFI_PASS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 启动失败: %s（链路按 DISCONNECTED 显示）", esp_err_to_name(err));
        return;
    }
    s_radio_started = true;
#if APP_IDLE_DOWNGRADE_ENABLED
    /* ZC9：dev_net_start 已按 CDT_WIFI_PS_MODE（MIN 基线）应用省电档；
     * 降档状态机随无线重启复位（动态 MAX 只在本次连接会话内演化）。 */
    s_idle_ps_max_applied = false;
    s_idle_max_entered_ms = 0;
#endif

    /* 等 IP（≤15s）；超时也起 WSS，靠其冻结退避自愈 */
    int waited = 0;
    while (dev_net_state() != DEV_NET_CONNECTED && waited < WIFI_WAIT_IP_MS) {
        vTaskDelay(pdMS_TO_TICKS(APP_TICK_MS * 10));
        waited += APP_TICK_MS * 10;
        dev_net_poll(now_ms());
    }
    char ip[16];
    ESP_LOGI(TAG, "[net] WiFi %s（ip=%s）",
             dev_net_state() == DEV_NET_CONNECTED ? "已连接" : "未取到 IP（继续）",
             dev_net_ip_str(ip, sizeof ip) ? ip : "-");

    static uint8_t spki_pin[32]; /* start 浅拷贝引用，须静态存储 */
    hex_to_pin(DEV_BRIDGE_SPKI_SHA256_HEX, spki_pin);

    static char uri[160];
    snprintf(uri, sizeof uri, "wss://%s:%d%s", DEV_BRIDGE_HOST, DEV_BRIDGE_PORT,
             CDT_WSS_PATH);
    static const char *ca = DEV_BRIDGE_CA_PEM; /* 同上：静态存储 */
    static const char *token = DEV_DEVICE_TOKEN;

    char fp[9];
    cdt_wss_token_fingerprint8(token, fp);
    ESP_LOGI(TAG, "[wss] 连接 %s（token 指纹 %s…，CA 验链 + SPKI pin）", uri, fp);

    s_wss_cfg = (cdt_wss_config_t){
        .user = NULL,
        .on_text = wss_on_text,
        .on_link = wss_on_link,
        .uri = uri,
        .token = token,
        .ca_pem = ca,
        .spki_sha256 = spki_pin,
        .buffer_size = 0,     /* 默认 2048 */
        .allow_insecure_ws = false, /* 生产红线：明文禁用（§5.1） */
    };
    err = cdt_wss_start(&s_wss_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WSS 启动失败: %s", esp_err_to_name(err));
    }
}

/* R4 新鲜度重同步：受控的主动 stop/start 一次。
 * 契约：INTERFACES §4「150秒无更新可标disconnected并重连」「收到相同seq的
 * 重复数据不能让旧状态永远新鲜」、§6「主机恢复后请求/接收最新全量」。
 * 审核条目：ARCHITECTURE_REVIEW_2026-09-11 R4（新鲜度重同步，不能只改显示）。
 * 触发域（app 主循环 link_check_and_resync）：仅限「wss 链路 CONNECTED（TCP/
 * TLS 健康，ping/pong 正常）但业务停摆 ≥LINK_DEAD_MS 无合法新快照」——transport
 * 自身断开/退避由 cdt_wss_client 冻结退避自愈（重连后 Bridge 重发全量，§6）；
 * CONFIG_ERROR 终态不重试（§5.4，等 cdt_wss_config_error_clear）。重同步即
 * stop/start：重置退避与聚合缓冲，重连后从握手 epoch 拿全量快照。 */
static void wss_resync(void)
{
    esp_err_t st = cdt_wss_stop();
    if (st == ESP_ERR_INVALID_STATE) {
        /* 未在运行：直接尝试重启（组件已自行退出时兜底） */
    } else if (st != ESP_OK) {
        /* stop 超时（≤6s 兜底未退出）：不再叠加第二个客户端任务，链路如实
         * 显示 DISCONNECTED，等下一观察窗再试。 */
        ESP_LOGE(TAG, "[resync] wss stop 失败: %s（本轮放弃，链路按断开显示）",
                 esp_err_to_name(st));
        return;
    }
    ESP_LOGW(TAG, "[resync] 业务停摆 ≥%ums 且链路健康 → wss stop/start 全量重同步",
             (unsigned)LINK_DEAD_MS);
    esp_err_t sr = cdt_wss_start(&s_wss_cfg);
    if (sr != ESP_OK) {
        ESP_LOGE(TAG, "[resync] wss start 失败: %s（链路按 DISCONNECTED 显示）",
                 esp_err_to_name(sr));
    }
}

/* ------------------------------------------------------------------ */
/* ZC9 空闲动态降档（P5.2 后半）：无合法新快照且无按键持续达阈值 →        */
/* MAX_MODEM；任何新快照/按键 → 立即回 MIN_MODEM（数据链路优先，红线）。   */
/* 决策单源 app_power_idle.c（宿主测试同源编译）；esp_wifi_set_ps 应用、  */
/* 档位去重与 INFO 日志在 dev_net.c（dev_net_set_idle_ps）。状态镜像       */
/* s_idle_* 见文件头部静态区；编译门 APP_IDLE_DOWNGRADE_ENABLED 见文件头。 */
/* 运行期门：无线已启且从未收到合法快照不降档（首包等待期保持 MIN）。      */
/* ------------------------------------------------------------------ */
#if APP_IDLE_DOWNGRADE_ENABLED

static void cdt_idle_downgrade_tick(int64_t now)
{
    if (!s_radio_started || !s_have_rx) {
        return;
    }
    int64_t threshold = (int64_t)CONFIG_CDT_IDLE_DOWNGRADE_MIN * 60000;
    /* app_power_idle.h 档位状态编码：>0=正处于 MAX（值为进入时刻）；
     * <0=不在 MAX（绝对值=最近一次进入时刻，供再入迟滞）。 */
    int64_t state = s_idle_ps_max_applied ? s_idle_max_entered_ms
                                          : -s_idle_max_entered_ms;
    app_power_idle_verdict_t v = app_power_idle_decide(
        now, (int64_t)s_last_rx_ms, (int64_t)s_last_key_ms, threshold, state);
    bool want_max = v.target == APP_POWER_IDLE_PS_MAX;
    if (!v.need_switch || want_max == s_idle_ps_max_applied) {
        return; /* 档位无变化：不调驱动不打日志（限频不刷屏） */
    }
    /* 触发原因（日志用）：回 MIN 取较新活动源；升 MAX 即空闲达标。 */
    const char *reason = want_max
                             ? "空闲>=阈值"
                             : ((int64_t)s_last_rx_ms >= (int64_t)s_last_key_ms
                                    ? "快照活动"
                                    : "按键活动");
    if (dev_net_set_idle_ps(want_max, reason) == ESP_OK) {
        s_idle_ps_max_applied = want_max;
        if (want_max) {
            s_idle_max_entered_ms = now;
        }
        /* 回 MIN：s_idle_max_entered_ms 保留（再入迟滞基准） */
    }
}
#endif /* APP_IDLE_DOWNGRADE_ENABLED */
#endif /* CDT_HAS_NET_CONFIG */

/* ------------------------------------------------------------------ */
/* app 主任务                                                            */
/* ------------------------------------------------------------------ */
static void app_task(void *arg)
{
    (void)arg;

    /* --- 显示先起：BOOT 期间就有画面 --- */
    st7305_config_t dcfg = st7305_default_config();
    esp_err_t err = st7305_init(&dcfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "st7305_init 失败: %s — 停机", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    ESP_ERROR_CHECK(cdt_lvgl_port_display_init());
    cdt_ui_init();
    cdt_nav_init(&s_nav, CDT_PAGE_NOW); /* §4：深睡重启回 NOW */
    runtime_update(now_ms());
    render(now_ms(), "boot");

    /* --- BOOT_CHECK：先 ADC 后无线（§7.3）--- */
    bool radio_ok = boot_check();
    render(now_ms(), "boot-check");
    /* ZC11：传输选择（Kconfig 编译期）。BLE 模式不起 WiFi/WSS，直接以冻结
     * UUID 广播（无凭据依赖，离线演示组合同样可用）；WIFI 模式走原路径。 */
    if (radio_ok && cdt_transport_sel_is_ble()) {
        cdt_transport_sel_ble_start();
    }
#if CDT_HAS_NET_CONFIG
    if (radio_ok && !cdt_transport_sel_is_ble()) {
        start_radio();
    } else if (!radio_ok) {
        ESP_LOGE(TAG, "BOOT_CHECK 受限：无线未启动（链接 DISCONNECTED 页，电压可看）");
    }
#else
    ESP_LOGW(TAG, "CDT_DEVNET_OFFLINE 演示构建：无网络凭据编译进固件，链路显示 DISCONNECTED");
#endif

    /* --- 主循环 --- */
    int64_t last_render = 0;
    int64_t last_sample = -1;
    int64_t last_wifi_poll = 0;
    cdt_wss_link_state_t logged_wss = CDT_WSS_LINK_DISCONNECTED;
    cdt_link_state_t logged_link = CDT_LINK_INVALID;

    for (;;) {
        int64_t now = now_ms();

        lv_timer_handler();

        /* KEY 事件（短按翻页/长按静音；P4.5 真实按键） */
        cdt_key_hw_event_t ev;
        bool need_render = false;
        while (xQueueReceive(s_key_queue, &ev, 0) == pdTRUE) {
            handle_key(ev, now, &need_render);
        }

        /* 快照收件槽 → StateStore（唯一解析任务） */
        handle_inbox(now, &need_render);

        /* 电池采样调度：10s 常规 / 1Hz 近阈值（§7.1；故障宽限期 1Hz 给恢复
         * 机会——R3b：invalid 样本必须能以合法节奏持续进 FSM）。
         * 裁决2 配套：boot_recovery_required（低压唤醒 hint + 首批采样失败的
         * recovery 稳定计时）也按 1Hz——FSM 连续性定义（cdt_power.c
         * handle_valid_sample）假设「计时在跑即 §7.1 1Hz 节奏」（间隔 >2s 构成
         * 缺测并重置 recov_running）；健康样本 >3750mV 会使前三个条件全假，
         * 10s 常规节奏每次采样都重置 recovery 稳定计时 → ALLOW 永不发出，
         * 运行期接线在该子路径不可达。 */
        bool fast = cdt_battery_should_sample_fast(
                        (s_have_sample && s_last_sample.valid) ? s_last_sample.battery_mv : 0) ||
                    s_fsm.state == CDT_POWER_LOW_WARN ||
                    s_fsm.state == CDT_POWER_CRITICAL ||
                    s_fsm.state == CDT_POWER_SLEEP_PREP ||
                    s_fsm.battery_fault ||
                    s_fsm.boot_recovery_required;
        int64_t period = fast ? BATTERY_PERIOD_FAST_MS : BATTERY_PERIOD_NORMAL_MS;
        if (last_sample < 0 || now - last_sample >= period) {
            last_sample = now;
            cdt_power_sample_t sample;
            esp_err_t serr = cdt_battery_sample_batch(now, &sample);
            if (serr == ESP_OK) {
                bool changed = !s_have_sample || s_last_sample.valid != sample.valid ||
                               s_last_sample.battery_mv != sample.battery_mv;
                s_last_sample = sample;
                s_have_sample = true;
                ESP_LOGI(TAG, "[battery] mv=%u valid=%d fast=%d", sample.battery_mv,
                         sample.valid, fast);
                if (changed) {
                    need_render = true; /* 电压上屏刷新 */
                }
            } else {
                /* R3b：整批失败必须以「当前时间、valid=false」样本进 FSM——
                 * 绝不把旧 valid 样本当新值复用（审核：旧代码失败分支仍送旧
                 * s_last_sample，invalid_streak 不推进、保护失效、UI 反复显示
                 * 旧健康电压）。s_last_sample 转为 invalid → runtime.battery_
                 * valid=false → presenter 电压位 "--"。恢复由下一批 ESP_OK。 */
                bool was_valid = s_have_sample && s_last_sample.valid;
                s_last_sample = app_power_invalid_sample(now);
                s_have_sample = true;
                if (was_valid) {
                    need_render = true; /* valid → unknown 转换上屏 */
                }
                ESP_LOGE(TAG, "[battery] 采样失败: %s（整批按 invalid 喂 FSM）",
                         esp_err_to_name(serr));
            }
            cdt_power_input_t in = {
                .kind = CDT_POWER_IN_SAMPLE,
                .sample = s_last_sample,
            };
            cdt_power_action_t acts = cdt_power_step(&s_fsm, &in, now);
            power_handle_actions(acts, now, &need_render);
#if CDT_HAS_NET_CONFIG
            /* 裁决2（app_power_policy.h 单源谓词）：ALLOW_RADIO_START 运行期接线
             * ——BOOT_CHECK 首批采样失败时无线未启动，主循环持续检测 FSM 启动
             * 判定；恢复样本使 FSM 发 ALLOW（首批恢复 / recovery 稳定 10s）时
             * 真正 start_radio（旧代码该动作仅日志，开无线请求被丢弃）。已启动
             * 不重复启动；睡眠请求锁存优先（同拍有睡眠动作不开无线）。BLOCK_
             * RADIO_START 属 BOOT_CHECK 域动作，运行期出现仅如实记日志。 */
            if (app_power_runtime_radio_allow(acts, s_radio_started) &&
                !s_sleep_latched) {
                ESP_LOGW(TAG, "[power] 运行期 ALLOW_RADIO_START → 启动无线");
                /* ZC11：BLE 模式起外设广播（无凭据/退避在 NimBLE 栈内）；
                 * WIFI 模式照旧 start_radio。 */
                if (cdt_transport_sel_is_ble()) {
                    cdt_transport_sel_ble_start();
                } else {
                    start_radio();
                }
                need_render = true;
            } else if (acts & CDT_POWER_ACT_BLOCK_RADIO_START) {
                ESP_LOGE(TAG, "[power] 运行期 BLOCK_RADIO_START（无线保持关闭）");
            }
#endif
            if (s_sleep_latched) {
                do_sleep_sequence(acts); /* R3a：锁存即执行；真机不返回 */
            }
        }

        /* WiFi 重连退避轮询（§5.4 序列；WSS 退避在组件内部任务）。
         * dev_net_poll 对未启动 STA 是 no-op（s_started 门卫），BLE 模式安全。 */
        if (now - last_wifi_poll >= 200) {
            last_wifi_poll = now;
            dev_net_poll(now);
        }

        /* ZC11：BLE 模式重组器超时巡检（3s 进展/30s 整包，NACK 组件内发出，
         * §3.3 接收方 4）；WIFI 模式 no-op。 */
        cdt_transport_sel_poll(now);

#if APP_IDLE_DOWNGRADE_ENABLED
        /* ZC9 空闲动态降档（P5.2 后半）：每拍纯决策；档位切换（esp_wifi_
         * set_ps + INFO 日志）只在 need_switch 时发生，天然限频。 */
        cdt_idle_downgrade_tick(now);
#endif

        /* 链路状态日志（WiFi/WSS 变化即打；新鲜度按 §4 推进） */
        if (s_wss_state != logged_wss) {
            logged_wss = s_wss_state;
            need_render = true;
        }
        cdt_link_state_t link = compute_link_state(now);
        if (link != logged_link) {
            ESP_LOGI(TAG, "[link] %d -> %d（last_rx=%s have_rx=%d wss=%d）",
                     (int)logged_link, (int)link,
                     s_have_rx ? "set" : "none", s_have_rx, (int)s_wss_state);
            logged_link = link;
            need_render = true;
        }

#if CDT_HAS_NET_CONFIG
        /* R4 新鲜度重同步（契约 INTERFACES §4/§6，审核 R4）：wss 链路 CONNECTED
         * （TCP/TLS 健康）但 ≥LINK_DEAD_MS 无合法新快照（含从未收到）→ 受控
         * stop/start 全量重同步，不是只改显示。transport 断开/退避由组件冻结
         * 退避自愈；CONFIG_ERROR 终态不在触发域。LINK_DEAD_MS 节流防高频。 */
        if (s_radio_started && s_wss_state == CDT_WSS_LINK_CONNECTED &&
            s_wss_connected_ms != 0 &&
            (uint32_t)(now - (int64_t)s_wss_connected_ms) >= LINK_DEAD_MS &&
            now - (int64_t)s_last_resync_ms >= (int64_t)LINK_DEAD_MS &&
            link == CDT_LINK_DISCONNECTED) {
            s_last_resync_ms = (uint32_t)now;
            wss_resync();
            need_render = true;
        }
#endif

        /* 渲染：事件驱动 + 1Hz 兜底（电压/时长推进） */
        if (need_render || now - last_render >= RENDER_PERIOD_MS) {
            last_render = now;
            render(now, need_render ? "event" : "1hz");
        }

        vTaskDelay(pdMS_TO_TICKS(APP_TICK_MS));
    }
}

/* ------------------------------------------------------------------ */
void app_main(void)
{
    /* NVS：WiFi 用（校准数据按 P4.4 决策不落 NVS，采样不写 Flash） */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    /* R4：收件槽锁必须在任何 on_text 回调（cdt_wss_start）之前就绪 */
    app_inbox_init();

    s_key_queue = xQueueCreate(4, sizeof(cdt_key_event_t));
    cdt_key_config_t kcfg = {
        .debounce_ms = 30,   /* §6 初值 30ms（集成期误填 0 导致轮询未启动，A0 修） */
        .long_press_ms = 800,/* §6 初值 800ms，松开时判定 */
        .poll_period_ms = 5, /* 5ms 轮询（P4.5 组件默认） */
        .on_event = key_cb,
        .user = NULL,
    };
    err = cdt_key_start(&kcfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cdt_key_start 失败: %s", esp_err_to_name(err));
    }

    BaseType_t ok = xTaskCreate(app_task, "cdt_app",
                                APP_TASK_STACK_BYTES / sizeof(StackType_t),
                                NULL, tskIDLE_PRIORITY + 5, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "create app task failed");
    }
}
