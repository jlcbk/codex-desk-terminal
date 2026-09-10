/*
 * test_p35_harness.c — P3.5 共同故障集成：设备侧「终点状态收敛」harness（host，无真机）
 *
 * 任务真源：docs/DEVELOPMENT_PLAN.md P3.5 行（UI 不回退；一次仅一个活动
 * transport；连接状态可观测）+ docs/INTERFACES.md §5（StateStore/Presenter
 * 语义）+ §4（链路新鲜度 45s stale / 150s disconnected、重复数据不续鲜）。
 *
 * 定位（任务书第 2 节「判定层级」）：设备侧终点 = 真实 shared C 内核——
 *   cdt_reassembler（BLE 分片重组，P3.4）→ cdt_state_store（seq/epoch 整包
 *   替换，P1.4）→ cdt_present（Presenter 纯转换，P2.1）。本 harness 不重实现
 *   任何传输/业务语义，只做三件事：
 *     ① 按注入容器逐条喂入：snapshot=完整快照字节（等价 WSS 设备侧收包）、
 *        fragment=完整 BLE DATA 帧（等价设备侧 ATT 写入）、probe=只观测；
 *     ② 维护 §4 链路新鲜度模型：仅「应用成功」的快照刷新 last_rx（同 seq
 *        重复不续鲜——§4 明文）；距 last_rx ≥45s → stale，≥150s →
 *        disconnected；应用成功即恢复 connected；
 *     ③ 每轮注入后调 cdt_present 得到 ViewModel，做「UI 不回退」判定：
 *        未应用轮（ignored/rejected/probe）的业务内容（project/activity/
 *        状态词）必须与上一轮一致（last-valid 保留）；applied 轮同 epoch seq
 *        必须严格递增；每轮 view、link 事件序列、applied 轨迹全部写入报告。
 *   Python 侧（同目录 test_*.py）构造「mock 快照序列 + 故障注入」，运行本
 *   harness 并对 JSON 报告断言终点收敛（断言传输细节最小化）。
 *
 * 产出报告 schema p35-harness-report-v1：
 *   rounds[i]{index,at_ms,action,link,reassemble,apply,seq,epoch,ack,
 *             threads,view{page,status,project,activity,time_frozen,
 *             link_stale,link_disconnected}}
 *   link_events[{at_ms,state}] / applied[{at_ms,epoch,seq,threads,projects}]
 *   store_final{has_state,epoch,seq} / link_final / ui_no_regress /
 *   no_regress_notes
 *
 * 范围红线：本文件是测试代码——不改协议、不改 shared/ 冻结文件；§4 新鲜度
 * 追踪为 harness 本地模型（真机由固件承担，P4/P5 实测项）。
 *
 * 用法：test_p35_harness --run CONTAINER REPORT_JSON
 * 退出码：0=容器处理完成（收敛与否在报告内，由 Python 断言）；2=环境/容器错误。
 *
 * 容器二进制格式（小端；由 p35_common.write_container 产出）：
 *   magic "P35C" | u16 version=1 | u16 mtu | u8 transport(0 mock,1 ble,2 wifi)
 *   | u8 rsv | u16 rsv
 *   每条目：u8 action(1=snapshot,2=ble_fragment,3=probe) | u8 rsv
 *           | u64 at_ms | u32 payload_len | payload 字节
 *   at_ms 必须单调不减（虚拟时钟，回退=容器错误）；fragment payload 为完整
 *   DATA 帧（16B 帧头+片载荷）；probe 的 payload_len=0。
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cdt_frame.h"
#include "cdt_reassembler.h"
#include "cdt_parser.h"
#include "cdt_store.h"
#include "cdt_presenter.h"

/* §4 新鲜度阈值（冻结值；测试经虚拟 at_ms 驱动，不缩放） */
#define P35_LINK_STALE_MS 45000
#define P35_LINK_DISCONNECTED_MS 150000

#define P35_MAGIC "P35C"
#define P35_CONTAINER_VERSION 1

#define P35_MAX_APPLIED 128
#define P35_MAX_LINK_EVENTS 64
#define P35_MAX_NOTES 8

/* ------------------------------------------------------------------ */
/* 小工具                                                               */
/* ------------------------------------------------------------------ */

static void die(const char *msg)
{
    fprintf(stderr, "FAIL(harness): %s\n", msg);
    exit(2);
}

/* 打印带引号转义的 JSON 字符串（UTF-8 原样透传，≥0x80 不转义） */
static void jstr(FILE *f, const char *s)
{
    fputc('"', f);
    for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p; p++) {
        unsigned char c = *p;
        if (c == '"' || c == '\\') {
            fputc('\\', f);
            fputc(c, f);
        } else if (c < 0x20) {
            fprintf(f, "\\u%04x", c);
        } else {
            fputc(c, f);
        }
    }
    fputc('"', f);
}

static const char *apply_name(cdt_parse_result_t r)
{
    switch (r) {
    case CDT_PARSE_OK: return "applied";
    case CDT_PARSE_IGNORED_STALE_SEQ: return "ignored_stale_seq";
    case CDT_PARSE_ERR_VERSION: return "err_version";
    case CDT_PARSE_ERR_SIZE: return "err_size";
    case CDT_PARSE_ERR_DEPTH: return "err_depth";
    case CDT_PARSE_ERR_FIELD: return "err_field";
    case CDT_PARSE_ERR_TRUNCATED_CP: return "err_truncated_cp";
    case CDT_PARSE_ERR_UNKNOWN_ENUM: return "err_unknown_enum";
    default: return "err_unknown";
    }
}

static const char *reject_name(cdt_reject_reason_t r)
{
    switch (r) {
    case CDT_REJECT_NONE: return "none";
    case CDT_REJECT_BAD_ARG: return "bad_arg";
    case CDT_REJECT_BAD_HEADER: return "bad_header";
    case CDT_REJECT_TOTAL_LEN_ZERO: return "total_len_zero";
    case CDT_REJECT_TOTAL_LEN_OVER: return "total_len_over";
    case CDT_REJECT_COUNT_OVER: return "count_over";
    case CDT_REJECT_GEOMETRY: return "geometry";
    case CDT_REJECT_INDEX_OOB: return "index_oob";
    case CDT_REJECT_BAD_FRAGMENT_SIZE: return "bad_fragment_size";
    case CDT_REJECT_CONTEXT_MISMATCH: return "context_mismatch";
    case CDT_REJECT_DATA_MISMATCH: return "data_mismatch";
    case CDT_REJECT_ORPHAN: return "orphan";
    case CDT_REJECT_CRC_MISMATCH: return "crc_mismatch";
    case CDT_REJECT_PROGRESS_TIMEOUT: return "progress_timeout";
    case CDT_REJECT_TOTAL_TIMEOUT: return "total_timeout";
    default: return "reject_unknown";
    }
}

static const char *page_name(cdt_page_t p)
{
    switch (p) {
    case CDT_PAGE_NOW: return "now";
    case CDT_PAGE_AGENTS: return "agents";
    case CDT_PAGE_PLAN: return "plan";
    case CDT_PAGE_USAGE: return "usage";
    case CDT_PAGE_LOW_BATTERY: return "low_battery";
    default: return "invalid";
    }
}

static const char *link_name(int s)
{
    switch (s) {
    case 1: return "connected";
    case 2: return "stale";
    case 3: return "disconnected";
    default: return "invalid";
    }
}

/* ------------------------------------------------------------------ */
/* harness 状态（单场景单进程；静态分配，同设备侧风格）                  */
/* ------------------------------------------------------------------ */

static cdt_state_store_t g_store;
static cdt_reassembler_t g_rs;

static int g_link = 1;      /* 1 connected / 2 stale / 3 disconnected */
static int64_t g_last_rx = 0; /* 最近一次「应用成功」快照的虚拟 ms（§4） */

static int64_t g_now = 0; /* 虚拟时钟（容器条目 at_ms，单调不减） */

/* applied 轨迹（Bridge 重启「全量替换无混合」断言用） */
typedef struct {
    int64_t at_ms;
    uint64_t seq;
    char epoch[CDT_MAX_BRIDGE_EPOCH_BYTES + 1];
    char threads[CDT_MAX_THREADS][CDT_MAX_ID_BYTES + 1];
    char projects[CDT_MAX_THREADS][CDT_MAX_PROJECT_BYTES + 1];
    uint8_t thread_count;
} p35_applied_t;

static p35_applied_t g_applied[P35_MAX_APPLIED];
static size_t g_applied_count = 0;

typedef struct {
    int64_t at_ms;
    int state;
} p35_link_event_t;

static p35_link_event_t g_link_events[P35_MAX_LINK_EVENTS];
static size_t g_link_event_count = 0;

/* 上一轮 view 业务内容（UI 不回退基准） */
static int g_prev_valid = 0;
static char g_prev_project[CDT_VIEW_PROJECT_BYTES];
static char g_prev_activity[CDT_VIEW_ACTIVITY_BYTES];
static char g_prev_status[CDT_VIEW_STATUS_BYTES];

/* applied 单调性基准（同 epoch 内 seq 严格递增） */
static int g_has_applied = 0;
static char g_applied_epoch[CDT_MAX_BRIDGE_EPOCH_BYTES + 1];
static uint64_t g_applied_seq = 0;

static int g_no_regress = 1;
static char g_notes[P35_MAX_NOTES][160];
static size_t g_note_count = 0;

static void note(const char *text)
{
    if (g_note_count < P35_MAX_NOTES) {
        snprintf(g_notes[g_note_count], sizeof(g_notes[0]), "%s", text);
        g_note_count++;
    }
    g_no_regress = 0;
}

/* ------------------------------------------------------------------ */
/* §4 新鲜度追踪（harness 本地模型；真机由固件承担）                     */
/* ------------------------------------------------------------------ */

static void set_link(int s, int64_t at_ms)
{
    if (g_link == s) {
        return;
    }
    g_link = s;
    if (g_link_event_count >= P35_MAX_LINK_EVENTS) {
        die("link event overflow (container too long for harness)");
    }
    g_link_events[g_link_event_count].at_ms = at_ms;
    g_link_events[g_link_event_count].state = s;
    g_link_event_count++;
}

static void freshness_observe(int64_t now)
{
    /* 观测点判定；恢复只能由「应用成功」触发（§4：有效新快照刷新 last_rx；
     * 同 seq 重复/低 seq 回退不得续鲜） */
    if (now - g_last_rx >= (int64_t)P35_LINK_DISCONNECTED_MS) {
        set_link(3, now);
    } else if (now - g_last_rx >= (int64_t)P35_LINK_STALE_MS) {
        set_link(2, now);
    }
}

static void freshness_on_applied(int64_t now)
{
    g_last_rx = now;
    set_link(1, now);
}

/* 场景起点：transport 视为已在首条目时刻连接（建模假设，报告首事件固定为
 * connected@首条目 at_ms；harness 模型不含"连接中"，断链由 Python 侧
 * LinkLog 记录） */
static int g_link_started = 0;

static void freshness_start(int64_t now)
{
    if (g_link_started) {
        return;
    }
    g_link_started = 1;
    if (g_link_event_count >= P35_MAX_LINK_EVENTS) {
        die("link event overflow (container too long for harness)");
    }
    g_link_events[g_link_event_count].at_ms = now;
    g_link_events[g_link_event_count].state = 1;
    g_link_event_count++;
}

/* ------------------------------------------------------------------ */
/* 重组器回调（交付 → 元数据解析 → store；三值结果驱动 ACK，§7）          */
/* ------------------------------------------------------------------ */

static uint8_t g_delivered[CDT_MAX_MESSAGE_LEN];
static size_t g_delivered_len;
static cdt_app_state_t g_meta; /* 观测元数据（独立解析；store 判定才是真值） */
static int g_meta_valid;
static cdt_parse_result_t g_delivery_apply;
static int g_delivery_applied; /* 本条目产生了 store 投递（snapshot 或完成重组） */
static cdt_reject_reason_t g_last_reject;

static void on_complete(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    if (len == 0 || len > sizeof(g_delivered)) {
        g_delivery_apply = CDT_PARSE_ERR_SIZE;
        g_delivery_applied = 1;
        return;
    }
    memcpy(g_delivered, bytes, len);
    g_delivered_len = len;
    g_meta_valid = (cdt_state_parse(g_delivered, g_delivered_len, &g_meta) == CDT_PARSE_OK);
    g_delivery_apply = cdt_state_store_apply(&g_store, g_delivered, g_delivered_len);
    g_delivery_applied = 1;
}

static void on_reject(void *user, cdt_reject_reason_t reason, const cdt_frame_header_t *hdr)
{
    (void)user;
    (void)hdr;
    /* P3.4 冻结行为（tests/transport/ble/test_transport.c
     * total_timeout_30s_trickle）：喂入触发超时的那一帧会先回调超时原因丢弃
     * 上下文，再对同一帧按"无上下文"补发一次 ORPHAN 事件（两次各可回一次
     * NACK）。终点报告只取首个原因（根因），避免二次事件掩盖真实故障。 */
    if (g_last_reject == CDT_REJECT_NONE) {
        g_last_reject = reason;
    }
}

/* ------------------------------------------------------------------ */
/* 每轮 view + UI 不回退判定 + round JSON                               */
/* ------------------------------------------------------------------ */

static void record_round(FILE *f, int index, const char *action, const char *reassemble,
                         const char *ack)
{
    const cdt_app_state_t *snap = cdt_state_store_snapshot(&g_store);

    cdt_runtime_t rt;
    memset(&rt, 0, sizeof(rt));
    rt.battery_valid = true;
    rt.battery_mv = 3900;
    rt.usable_percent = 60;
    rt.charging = CDT_PRESENCE_NO;
    rt.external_power = CDT_PRESENCE_NO;
    rt.power_state = CDT_POWER_ACTIVE;
    snprintf(rt.transport, sizeof(rt.transport), "mock");
    rt.link_state = (cdt_link_state_t)g_link; /* 枚举值 1/2/3 与 cdt_runtime.h 一致 */
    rt.last_rx_monotonic_ms = (uint32_t)g_last_rx;
    rt.selected_page = CDT_PAGE_NOW;

    cdt_view_t view;
    cdt_present(snap, &rt, (uint32_t)g_now, &view);

    /* ---- UI 不回退判定（判定层级：真实 presenter 输出） ---- */
    if (g_delivery_applied && g_delivery_apply == CDT_PARSE_OK) {
        if (g_has_applied && strcmp(g_store.epoch, g_applied_epoch) == 0 &&
            g_store.last_seq <= g_applied_seq) {
            char tmp[160];
            snprintf(tmp, sizeof(tmp),
                     "applied seq regress within epoch (round %d): %llu <= %llu", index,
                     (unsigned long long)g_store.last_seq, (unsigned long long)g_applied_seq);
            note(tmp);
        }
        g_has_applied = 1;
        snprintf(g_applied_epoch, sizeof(g_applied_epoch), "%s", g_store.epoch);
        g_applied_seq = g_store.last_seq;
        g_prev_valid = 1;
        snprintf(g_prev_project, sizeof(g_prev_project), "%s", view.project);
        snprintf(g_prev_activity, sizeof(g_prev_activity), "%s", view.activity);
        snprintf(g_prev_status, sizeof(g_prev_status), "%s", view.status_label);
    } else if (g_prev_valid) {
        /* 未应用轮：业务内容必须保持 last-valid（重复/拒绝/探测不回退 UI） */
        if (strcmp(view.project, g_prev_project) != 0 ||
            strcmp(view.activity, g_prev_activity) != 0 ||
            strcmp(view.status_label, g_prev_status) != 0) {
            char tmp[160];
            snprintf(tmp, sizeof(tmp), "view content changed on non-applied round %d", index);
            note(tmp);
        }
    }

    /* ---- round JSON ---- */
    fprintf(f, "%s    {\"index\":%d,\"at_ms\":%lld,\"action\":", index ? ",\n" : "", index,
            (long long)g_now);
    jstr(f, action);
    fprintf(f, ",\"link\":");
    jstr(f, link_name(g_link));
    fprintf(f, ",\"reassemble\":");
    jstr(f, reassemble);
    fprintf(f, ",\"apply\":");
    if (g_delivery_applied) {
        jstr(f, apply_name(g_delivery_apply));
    } else {
        jstr(f, "none");
    }
    if (g_delivery_applied && g_meta_valid) {
        fprintf(f, ",\"seq\":%llu,\"epoch\":", (unsigned long long)g_meta.seq);
        jstr(f, g_meta.bridge_epoch);
    } else {
        fprintf(f, ",\"seq\":null,\"epoch\":null");
    }
    fprintf(f, ",\"ack\":");
    jstr(f, ack);
    if (g_delivery_applied && g_delivery_apply == CDT_PARSE_OK && g_meta_valid) {
        fprintf(f, ",\"threads\":[");
        for (int i = 0; i < g_meta.thread_count; i++) {
            if (i) {
                fputc(',', f);
            }
            jstr(f, g_meta.threads[i].id);
        }
        fprintf(f, "]");
    } else {
        fprintf(f, ",\"threads\":null");
    }
    fprintf(f, ",\"view\":{\"page\":");
    jstr(f, page_name(view.page));
    fprintf(f, ",\"status\":");
    jstr(f, view.status_label);
    fprintf(f, ",\"project\":");
    jstr(f, view.project);
    fprintf(f, ",\"activity\":");
    jstr(f, view.activity);
    fprintf(f, ",\"time_frozen\":%s,\"link_stale\":%s,\"link_disconnected\":%s",
            view.time_frozen ? "true" : "false", view.link_stale ? "true" : "false",
            view.link_disconnected ? "true" : "false");
    fprintf(f, "}}");

    /* ---- applied 轨迹 ---- */
    if (g_delivery_applied && g_delivery_apply == CDT_PARSE_OK) {
        if (g_applied_count >= P35_MAX_APPLIED) {
            die("applied overflow (container too long for harness)");
        }
        p35_applied_t *a = &g_applied[g_applied_count++];
        a->at_ms = g_now;
        a->seq = g_store.last_seq;
        snprintf(a->epoch, sizeof(a->epoch), "%s", g_store.epoch);
        a->thread_count = 0;
        if (g_meta_valid) {
            for (int i = 0; i < g_meta.thread_count && i < CDT_MAX_THREADS; i++) {
                snprintf(a->threads[i], sizeof(a->threads[i]), "%s", g_meta.threads[i].id);
                snprintf(a->projects[i], sizeof(a->projects[i]), "%s",
                         g_meta.threads[i].project);
            }
            a->thread_count = (uint8_t)g_meta.thread_count;
        }
    }
}

/* ------------------------------------------------------------------ */
/* 条目处理                                                             */
/* ------------------------------------------------------------------ */

static const uint8_t *g_buf;
static size_t g_len;
static size_t g_pos;

static void need(size_t n)
{
    if (g_len - g_pos < n) {
        die("container truncated");
    }
}

static uint8_t rd_u8(void)
{
    need(1);
    return g_buf[g_pos++];
}

static uint16_t rd_u16(void)
{
    need(2);
    uint16_t v = (uint16_t)(g_buf[g_pos] | ((uint16_t)g_buf[g_pos + 1] << 8));
    g_pos += 2;
    return v;
}

static uint32_t rd_u32(void)
{
    need(4);
    uint32_t v = (uint32_t)g_buf[g_pos] | ((uint32_t)g_buf[g_pos + 1] << 8) |
                 ((uint32_t)g_buf[g_pos + 2] << 16) | ((uint32_t)g_buf[g_pos + 3] << 24);
    g_pos += 4;
    return v;
}

static uint64_t rd_u64(void)
{
    uint64_t lo = rd_u32();
    uint64_t hi = rd_u32();
    return lo | (hi << 32);
}

int main(int argc, char **argv)
{
    if (argc != 4 || strcmp(argv[1], "--run") != 0) {
        fprintf(stderr, "usage: %s --run CONTAINER REPORT_JSON\n", argv[0]);
        return 2;
    }

    /* 读容器 */
    FILE *in = fopen(argv[2], "rb");
    if (in == NULL) {
        die("cannot open container");
    }
    if (fseek(in, 0, SEEK_END) != 0) {
        die("seek failed");
    }
    long fsz = ftell(in);
    if (fsz <= 0 || (unsigned long)fsz > 16UL * 1024UL * 1024UL) {
        die("container size invalid");
    }
    rewind(in);
    static uint8_t container[16UL * 1024UL * 1024UL + 1024];
    if (fread(container, 1, (size_t)fsz, in) != (size_t)fsz) {
        die("read container failed");
    }
    fclose(in);
    g_buf = container;
    g_len = (size_t)fsz;
    g_pos = 0;

    need(12);
    if (memcmp(g_buf, P35_MAGIC, 4) != 0) {
        die("bad magic");
    }
    g_pos = 4;
    uint16_t ver = rd_u16();
    if (ver != P35_CONTAINER_VERSION) {
        die("container version unsupported");
    }
    uint16_t mtu = rd_u16();
    uint8_t transport_code = rd_u8();
    (void)rd_u8();  /* rsv */
    (void)rd_u16(); /* rsv */
    const char *transport_name =
        (transport_code == 1) ? "ble" : (transport_code == 2) ? "wifi" : "mock";
    if (transport_code > 2) {
        die("bad transport code");
    }

    /* 设备侧内核初始化 */
    cdt_state_store_init(&g_store);
    cdt_reassembler_cbs_t cbs;
    cbs.user = NULL;
    cbs.on_complete = on_complete;
    cbs.on_reject = on_reject;
    if (!cdt_reassembler_init(&g_rs, mtu, &cbs)) {
        die("reassembler init failed (bad mtu)");
    }

    FILE *f = fopen(argv[3], "wb");
    if (f == NULL) {
        die("cannot open report for write");
    }
    fprintf(f, "{\n  \"schema\": \"p35-harness-report-v1\",\n  \"mtu\": %u,\n  \"transport\": ",
            (unsigned)mtu);
    jstr(f, transport_name);
    fprintf(f, ",\n  \"rounds\": [\n");

    int index = 0;
    while (g_pos < g_len) {
        uint8_t action = rd_u8();
        (void)rd_u8(); /* rsv */
        uint64_t at_ms = rd_u64();
        uint32_t plen = rd_u32();
        need(plen);
        const uint8_t *payload = g_buf + g_pos;
        g_pos += plen;

        if ((int64_t)at_ms < g_now) {
            die("container at_ms regressed (virtual clock must be monotonic)");
        }
        g_now = (int64_t)at_ms;
        freshness_start(g_now);
        freshness_observe(g_now);

        /* 每条目投递状态复位 */
        g_delivery_applied = 0;
        g_delivery_apply = CDT_PARSE_OK;
        g_meta_valid = 0;
        g_delivered_len = 0;
        g_last_reject = CDT_REJECT_NONE;

        const char *reassemble = "na";
        const char *ack = "none";

        if (action == 1) { /* snapshot：WSS 设备侧等价路径（完整快照 → store） */
            if (plen == 0 || plen > CDT_STATE_JSON_MAX_BYTES) {
                g_delivery_apply = CDT_PARSE_ERR_SIZE;
                g_delivery_applied = 1;
            } else {
                memcpy(g_delivered, payload, plen);
                g_delivered_len = plen;
                g_meta_valid = (cdt_state_parse(g_delivered, g_delivered_len, &g_meta) ==
                                CDT_PARSE_OK);
                g_delivery_apply =
                    cdt_state_store_apply(&g_store, g_delivered, g_delivered_len);
                g_delivery_applied = 1;
            }
        } else if (action == 2) { /* ble_fragment：完整 DATA 帧 → 重组器 */
            if (plen < CDT_FRAME_SIZE) {
                reassemble = "rejected:bad_header";
                ack = "nack";
            } else {
                cdt_frame_header_t hdr;
                cdt_frame_decode(payload, &hdr);
                cdt_feed_result_t fr = cdt_reassembler_feed(
                    &g_rs, &hdr, payload + CDT_FRAME_SIZE, plen - CDT_FRAME_SIZE, g_now);
                if (fr == CDT_FEED_OK) {
                    reassemble = "ok";
                } else if (fr == CDT_FEED_COMPLETED) {
                    reassemble = "completed";
                } else if (fr == CDT_FEED_REJECTED) {
                    static char rbuf[48];
                    snprintf(rbuf, sizeof(rbuf), "rejected:%s", reject_name(g_last_reject));
                    reassemble = rbuf;
                } else {
                    reassemble = "arg_err";
                }
                if (fr == CDT_FEED_REJECTED) {
                    ack = "nack"; /* §7：rejected → NACK（重发全包） */
                } else if (g_delivery_applied) {
                    /* completed → store：applied=ACK；duplicate（IGNORED_STALE_SEQ）
                     * =ACK 但不渲染；解析失败=NACK */
                    ack = (g_delivery_apply == CDT_PARSE_OK ||
                           g_delivery_apply == CDT_PARSE_IGNORED_STALE_SEQ)
                              ? "ack"
                              : "nack";
                }
            }
        } else if (action == 3) { /* probe：只观测（链路/保留状态证据点） */
            /* 无投递、无 ACK */
        } else {
            die("bad action code in container");
        }

        if (g_delivery_applied && g_delivery_apply == CDT_PARSE_OK) {
            freshness_on_applied(g_now); /* 应用成功：刷新 last_rx、恢复 connected */
        }

        record_round(f, index, (action == 1) ? "snapshot" : (action == 2) ? "fragment" : "probe",
                     reassemble, ack);
        index++;
    }

    /* ---- 报告尾部 ---- */
    const cdt_app_state_t *snap = cdt_state_store_snapshot(&g_store);
    fprintf(f, "\n  ],\n  \"link_events\": [");
    for (size_t i = 0; i < g_link_event_count; i++) {
        fprintf(f, "%s{\"at_ms\":%lld,\"state\":", i ? ", " : "",
                (long long)g_link_events[i].at_ms);
        jstr(f, link_name(g_link_events[i].state));
        fputc('}', f);
    }
    fprintf(f, "],\n  \"applied\": [");
    for (size_t i = 0; i < g_applied_count; i++) {
        p35_applied_t *a = &g_applied[i];
        fprintf(f, "%s{\"at_ms\":%lld,\"epoch\":", i ? ", " : "", (long long)a->at_ms);
        jstr(f, a->epoch);
        fprintf(f, ",\"seq\":%llu,\"threads\":[", (unsigned long long)a->seq);
        for (int t = 0; t < a->thread_count; t++) {
            fprintf(f, "%s", t ? ", " : "");
            jstr(f, a->threads[t]);
        }
        fprintf(f, "],\"projects\":[");
        for (int t = 0; t < a->thread_count; t++) {
            fprintf(f, "%s", t ? ", " : "");
            jstr(f, a->projects[t]);
        }
        fprintf(f, "]}");
    }
    fprintf(f, "],\n  \"store_final\": {\"has_state\":%s,\"epoch\":",
            snap ? "true" : "false");
    if (snap) {
        jstr(f, g_store.epoch);
        fprintf(f, ",\"seq\":%llu", (unsigned long long)g_store.last_seq);
    } else {
        fprintf(f, "null,\"seq\":null");
    }
    fprintf(f, "},\n  \"link_final\": ");
    jstr(f, link_name(g_link));
    fprintf(f, ",\n  \"ui_no_regress\": %s,\n  \"no_regress_notes\": [", g_no_regress ? "true" : "false");
    for (size_t i = 0; i < g_note_count; i++) {
        fprintf(f, "%s", i ? ", " : "");
        jstr(f, g_notes[i]);
    }
    fprintf(f, "]\n}\n");
    if (fclose(f) != 0) {
        die("write report failed");
    }
    return 0;
}
