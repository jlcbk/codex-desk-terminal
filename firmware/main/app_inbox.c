/*
 * app_inbox.c — WSS→app 任务收件槽实现（R4）
 *
 * 审核条目：docs/ARCHITECTURE_REVIEW_2026-09-11.md R4。
 * 旧实现（main.c 165–176/338–346）：WSS 回调与主任务对同一 s_inbox 无锁共享，
 * volatile 标志不提供互斥——解析旧快照期间新消息可覆盖缓冲（混包/丢 pending）。
 * 现实现：互斥锁短临界区内完成「生产入槽 / 槽→消费者独享缓冲复制」，解析在
 * 锁外进行（审核 R4 最小方向：短时加锁复制到消费者独享缓冲，再解锁解析）。
 *
 * 缓冲预算：普通槽 + 优先槽各 16KiB（PSRAM 静态区，与 store 的 ~34KiB 同域；
 * 消费者独享缓冲由 main.c 持有，同 PSRAM）。三个缓冲合计 48KiB，板载 OCT
 * PSRAM（sdkconfig CONFIG_SPIRAM_MODE_OCT）余量充足。
 */
#include "app_inbox.h"

#include <string.h>

#ifdef CDT_APP_INBOX_HOST
/* 宿主测试构建：pthread 锁编译同一交接逻辑（scripts/build_integration_fix_tests.sh） */
#include <pthread.h>

#define APP_INBOX_ATTR static
static pthread_mutex_t s_mu = PTHREAD_MUTEX_INITIALIZER;

static void inbox_lock(void)
{
    pthread_mutex_lock(&s_mu);
}

static void inbox_unlock(void)
{
    pthread_mutex_unlock(&s_mu);
}
#else
/* 固件构建：FreeRTOS 互斥锁（wss 任务 ↔ app 任务）；大缓冲落 PSRAM 静态区 */
#include "esp_attr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define APP_INBOX_ATTR EXT_RAM_BSS_ATTR static
static SemaphoreHandle_t s_mu;

static void inbox_lock(void)
{
    xSemaphoreTake(s_mu, portMAX_DELAY);
}

static void inbox_unlock(void)
{
    xSemaphoreGive(s_mu);
}
#endif

/* 生产者侧槽（锁内独占写；消费者只在锁内读并在取出时清占用） */
APP_INBOX_ATTR uint8_t s_slot_normal[APP_INBOX_MSG_MAX];
APP_INBOX_ATTR uint8_t s_slot_priority[APP_INBOX_MSG_MAX];
/* 占用长度：0=空；仅锁内读写（互斥提供可见性与原子性，不需要 volatile） */
static size_t s_normal_len;
static size_t s_priority_len;

/* 计数（锁内更新；§5：超预算必须计数） */
static uint32_t s_dropped_normal;   /* 普通槽 capacity-1 覆盖（原 s_inbox_dropped 语义） */
static uint32_t s_dropped_priority; /* 优先槽被新提醒覆盖 */
static uint32_t s_delivered;        /* on_text 完整消息交付计数（原 s_req_seq） */

void app_inbox_init(void)
{
#ifndef CDT_APP_INBOX_HOST
    if (s_mu == NULL) {
        s_mu = xSemaphoreCreateMutex();
    }
#endif
}

/* ---- 优先判定（纯函数；语义见 app_inbox.h 注释）---- */

static bool is_ws(unsigned char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

bool app_inbox_is_priority(const uint8_t *bytes, size_t len)
{
    /* 扫描 `"state"` → 可选空白 → ':' → 可选空白 → '"' → needs_you"/error"。 */
    static const char key[] = "\"state\"";
    const size_t key_len = sizeof(key) - 1;
    size_t i;

    if (bytes == NULL) {
        return false;
    }
    for (i = 0; i + key_len <= len; i++) {
        size_t j;
        if (bytes[i] != '"' || memcmp(bytes + i, key, key_len) != 0) {
            continue;
        }
        j = i + key_len;
        while (j < len && is_ws(bytes[j])) {
            j++;
        }
        if (j >= len || bytes[j] != ':') {
            continue;
        }
        j++;
        while (j < len && is_ws(bytes[j])) {
            j++;
        }
        if (j >= len || bytes[j] != '"') {
            continue;
        }
        j++;
        if (j + 10u <= len && memcmp(bytes + j, "needs_you\"", 10) == 0) {
            return true;
        }
        if (j + 6u <= len && memcmp(bytes + j, "error\"", 6) == 0) {
            return true;
        }
    }
    return false;
}

/* ---- 生产者（wss 任务上下文）---- */

void app_inbox_produce(const uint8_t *bytes, size_t len)
{
    bool prio;

    if (bytes == NULL || len == 0u || len > APP_INBOX_MSG_MAX) {
        return; /* 聚合组件已保证 ≤16KiB；防御 */
    }
    prio = app_inbox_is_priority(bytes, len);

    inbox_lock();
    if (prio) {
        if (s_priority_len != 0u) {
            s_dropped_priority++; /* 有界优先槽：新提醒顶掉未呈现旧提醒，必须计数 */
        }
        memcpy(s_slot_priority, bytes, len);
        s_priority_len = len;
    } else {
        if (s_normal_len != 0u) {
            s_dropped_normal++; /* capacity-1 覆盖计数（§5 语义保留） */
        }
        memcpy(s_slot_normal, bytes, len);
        s_normal_len = len;
    }
    s_delivered++;
    inbox_unlock();
}

/* ---- 消费者（唯一解析任务）---- */

bool app_inbox_take(uint8_t *dst, size_t cap, size_t *len_out)
{
    bool ok = false;

    if (dst == NULL || len_out == NULL) {
        return false;
    }
    inbox_lock();
    /* 优先槽先出（INTERFACES §5「先呈现后续终态」）：提醒转换未被呈现前，
     * 普通 DONE 只进普通槽，不顶掉提醒。cap 不足则消息留在槽内（不丢，
     * 防御路径——随船消费者缓冲 = APP_INBOX_MSG_MAX，不会发生）。 */
    if (s_priority_len != 0u && s_priority_len <= cap) {
        memcpy(dst, s_slot_priority, s_priority_len);
        *len_out = s_priority_len;
        s_priority_len = 0u;
        ok = true;
    } else if (s_normal_len != 0u && s_normal_len <= cap) {
        memcpy(dst, s_slot_normal, s_normal_len);
        *len_out = s_normal_len;
        s_normal_len = 0u;
        ok = true;
    }
    inbox_unlock();
    return ok;
}

uint32_t app_inbox_dropped_normal(void)
{
    uint32_t v;
    inbox_lock();
    v = s_dropped_normal;
    inbox_unlock();
    return v;
}

uint32_t app_inbox_dropped_priority(void)
{
    uint32_t v;
    inbox_lock();
    v = s_dropped_priority;
    inbox_unlock();
    return v;
}

uint32_t app_inbox_delivered(void)
{
    uint32_t v;
    inbox_lock();
    v = s_delivered;
    inbox_unlock();
    return v;
}
