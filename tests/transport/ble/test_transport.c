/*
 * test_transport.c — P3.4（A4-B，host 阶段）BLE 分片/重组引擎 C 宿主测试
 *
 * 真源：docs/INTERFACES.md §7、protocol/transport.md §3/§4（冻结）、
 * docs/DEVELOPMENT_PLAN.md P3.4 行（最小 MTU/大包/断连半包不破坏 store；
 * 重连重发全量）。
 *
 * 用法：
 *   test_transport                          # 全部内置用例（CRC 4 冻结向量、
 *                                           # 帧编解码、分片/重组矩阵、
 *                                           # 拒绝路径、断连清空、超时、ACK/NACK）
 *   test_transport --loopback FRAG BIN_ORIG OUT MTU
 *                                           # loopback 接收端：读长度前缀帧
 *                                           # 文件→重组→逐字节比对→写 OUT→
 *                                           # 喂 StateStore（applied/duplicate
 *                                           # →ACK/NACK 决策辅助）
 *   test_transport --emit-fragments ORIG MTU MSGID OUT
 *                                           # loopback 反向：C 分片器产出帧文件
 *                                           # （与 Python 重组互验）
 * 退出码 0 = 全部通过，1 = 任一失败。
 *
 * 说明：Transport 不解析业务 JSON；store/JSON 校验只发生在测试代码
 * （链接 shared/state，红线允许的宿主侧验证）。超时用例全部使用虚拟
 * now_ms，无真实等待。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cdt_frame.h"
#include "cdt_fragmenter.h"
#include "cdt_reassembler.h"
#include "cdt_store.h"

static int g_failures = 0;
static int g_passes = 0;

static void check(int cond, const char *name, const char *detail)
{
    if (cond) {
        g_passes++;
        printf("[PASS] %s\n", name);
    } else {
        g_failures++;
        printf("[FAIL] %s — %s\n", name, detail ? detail : "");
    }
}

/* ------------------------------------------------------------------ */
/* 回调记录器                                                          */
/* ------------------------------------------------------------------ */

static uint8_t g_completed[CDT_MAX_MESSAGE_LEN];
static size_t g_completed_len = 0;
static int g_complete_count = 0;
static cdt_reject_reason_t g_last_reason = CDT_REJECT_NONE;
static cdt_frame_header_t g_last_reject_hdr;
static int g_reject_count = 0;
static cdt_reject_reason_t g_reject_log[16];
static int g_reject_log_n = 0;

static void rec_on_complete(void *user, const uint8_t *bytes, size_t len)
{
    (void)user;
    if (len <= CDT_MAX_MESSAGE_LEN) {
        memcpy(g_completed, bytes, len);
    }
    g_completed_len = len;
    g_complete_count++;
}

static void rec_on_reject(void *user, cdt_reject_reason_t reason,
                          const cdt_frame_header_t *hdr)
{
    (void)user;
    g_last_reason = reason;
    if (g_reject_log_n < (int)(sizeof(g_reject_log) / sizeof(g_reject_log[0]))) {
        g_reject_log[g_reject_log_n++] = reason;
    }
    if (hdr != NULL) {
        g_last_reject_hdr = *hdr;
    }
    g_reject_count++;
}

static void rec_reset(void)
{
    g_completed_len = 0;
    g_complete_count = 0;
    g_last_reason = CDT_REJECT_NONE;
    memset(&g_last_reject_hdr, 0, sizeof(g_last_reject_hdr));
    g_reject_count = 0;
    g_reject_log_n = 0;
}

static cdt_reassembler_cbs_t rec_cbs(void)
{
    cdt_reassembler_cbs_t c;
    c.user = NULL;
    c.on_complete = rec_on_complete;
    c.on_reject = rec_on_reject;
    return c;
}

/* ------------------------------------------------------------------ */
/* 辅助：确定性 pattern、分片→（乱序/重复）喂入                         */
/* ------------------------------------------------------------------ */

static void fill_pattern(uint8_t *buf, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        buf[i] = (uint8_t)(i & 0xFFu);
    }
}

/* transport.md §4 V3 的冻结构造：bytes(range(256)) * 64 */
static void fill_pattern_16k(uint8_t *buf)
{
    for (int rep = 0; rep < 64; rep++) {
        for (int b = 0; b < 256; b++) {
            buf[rep * 256 + b] = (uint8_t)b;
        }
    }
}

#define MAX_TEST_FRAMES CDT_MAX_FRAGMENTS
static uint8_t g_emit[CDT_MAX_MESSAGE_LEN + (size_t)CDT_MAX_FRAGMENTS * CDT_FRAME_SIZE];
static size_t g_emit_off[MAX_TEST_FRAMES];
static uint16_t g_emit_len[MAX_TEST_FRAMES];
static size_t g_emit_count;

/* 用 C 分片器把 msg 产出到 g_emit（帧=16B 头+载荷）；返回片数。 */
static size_t build_frames(const uint8_t *msg, size_t len, uint32_t msg_id,
                           uint16_t mtu)
{
    cdt_fragmenter_t fg;
    g_emit_count = 0;
    if (!cdt_fragmenter_begin(&fg, msg, len, msg_id, mtu)) {
        return 0;
    }
    size_t cursor = 0;
    while (!cdt_fragmenter_done(&fg)) {
        size_t n = cdt_fragmenter_next(&fg, g_emit + cursor,
                                       sizeof(g_emit) - cursor);
        if (n == 0) {
            return 0;
        }
        g_emit_off[g_emit_count] = cursor;
        g_emit_len[g_emit_count] = (uint16_t)n;
        cursor += n;
        g_emit_count++;
    }
    return g_emit_count;
}

static cdt_feed_result_t feed_frame(cdt_reassembler_t *rs, const uint8_t *frame,
                                    size_t flen, int64_t now_ms)
{
    cdt_frame_header_t h;
    cdt_frame_decode(frame, &h);
    return cdt_reassembler_feed(rs, &h, frame + CDT_FRAME_SIZE,
                                flen - CDT_FRAME_SIZE, now_ms);
}

/* 顺序/乱序/重复喂入全部帧；返回最后一个 feed 结果。 */
static cdt_feed_result_t feed_frames(cdt_reassembler_t *rs, size_t from, size_t to,
                                     int step, int repeat, int64_t t0, int64_t dt)
{
    /* 完成即停：交付后引擎无上下文，继续喂剩余片只会产生孤儿拒绝，
     * 不属于被测语义。 */
    cdt_feed_result_t r = CDT_FEED_OK;
    int64_t t = t0;
    if (step > 0) {
        for (size_t i = from; i < to; i++) {
            for (int k = 0; k < repeat; k++) {
                r = feed_frame(rs, g_emit + g_emit_off[i], g_emit_len[i], t);
                if (r == CDT_FEED_COMPLETED) {
                    return r;
                }
            }
            t += dt;
        }
    } else {
        for (size_t i = to; i-- > from;) {
            for (int k = 0; k < repeat; k++) {
                r = feed_frame(rs, g_emit + g_emit_off[i], g_emit_len[i], t);
                if (r == CDT_FEED_COMPLETED) {
                    return r;
                }
            }
            t += dt;
        }
    }
    return r;
}

/* ------------------------------------------------------------------ */
/* A. CRC32：protocol/transport.md §4 四个冻结向量                     */
/* ------------------------------------------------------------------ */

static void test_crc_frozen_vectors(void)
{
    /* V1 empty_payload: b"" → 0x00000000 */
    check(cdt_crc32(NULL, 0) == 0x00000000u, "crc_v1_empty_payload",
          "cdt_crc32(NULL,0) != 0x00000000");
    static const uint8_t empty[1] = {0};
    check(cdt_crc32(empty, 0) == 0x00000000u, "crc_v1_empty_buffer",
          "cdt_crc32(buf,0) != 0x00000000");

    /* V2 short_json: b'{"kind":"state","seq":1}'（24B）→ 0x22B3AE57 */
    static const uint8_t v2[] = "{\"kind\":\"state\",\"seq\":1}";
    check(sizeof(v2) - 1 == 24, "crc_v2_payload_len_24", "短 JSON 长度不是 24");
    check(cdt_crc32(v2, sizeof(v2) - 1) == 0x22B3AE57u, "crc_v2_short_json",
          "V2 != 0x22B3AE57");

    /* V3 pattern_16k: bytes(range(256))*64（16384B）→ 0xE81722F0 */
    static uint8_t v3[16384];
    fill_pattern_16k(v3);
    check(cdt_crc32(v3, sizeof(v3)) == 0xE81722F0u, "crc_v3_pattern_16k",
          "V3 != 0xE81722F0");

    /* V4 fox_reference: 43B → 0x414FA339（公开文献参照值） */
    static const uint8_t v4[] = "The quick brown fox jumps over the lazy dog";
    check(sizeof(v4) - 1 == 43, "crc_v4_payload_len_43", "fox 长度不是 43");
    check(cdt_crc32(v4, sizeof(v4) - 1) == 0x414FA339u, "crc_v4_fox_reference",
          "V4 != 0x414FA339");

    /* 实现定义哨兵：data==NULL 且 len>0 → 非 0 错误值（见 cdt_frame.h） */
    check(cdt_crc32(NULL, 5) == 0xDEADBEEFu, "crc_null_len5_sentinel",
          "NULL 指针哨兵值与实现契约不符");
}

/* ------------------------------------------------------------------ */
/* B. 帧编解码与静态校验（cdt_frame.h P0.5 声明，P3.4 实现）            */
/* ------------------------------------------------------------------ */

static void test_frame_codec(void)
{
    cdt_frame_header_t h;
    h.version = 1;
    h.type = CDT_FRAME_TYPE_DATA;
    h.message_id = 0xA1B2C3D4u;
    h.fragment_index = 0x1234u;
    h.fragment_count = 0x5678u;
    h.total_len = 0x9ABCu;
    h.crc32 = 0xDEADBEEFu;

    uint8_t wire[CDT_FRAME_SIZE];
    memset(wire, 0, sizeof(wire));
    cdt_frame_encode(&h, wire);

    /* 字节偏移冻结（transport.md §3.1）：小端逐字节核对 */
    int layout_ok = wire[0] == 0x01 && wire[1] == 0x01 &&
                    wire[2] == 0xD4 && wire[3] == 0xC3 &&
                    wire[4] == 0xB2 && wire[5] == 0xA1 &&
                    wire[6] == 0x34 && wire[7] == 0x12 &&
                    wire[8] == 0x78 && wire[9] == 0x56 &&
                    wire[10] == 0xBC && wire[11] == 0x9A &&
                    wire[12] == 0xEF && wire[13] == 0xBE &&
                    wire[14] == 0xAD && wire[15] == 0xDE;
    check(layout_ok, "frame_encode_byte_layout", "16 字节小端布局与 §3.1 不符");

    cdt_frame_header_t back;
    cdt_frame_decode(wire, &back);
    check(memcmp(&h, &back, sizeof(h)) == 0, "frame_decode_roundtrip",
          "decode(encode(h)) != h");

    /* 静态校验：合法 DATA */
    cdt_frame_header_t ok = h;
    ok.version = CDT_FRAME_VERSION;
    ok.type = CDT_FRAME_TYPE_DATA;
    ok.fragment_index = 3;
    ok.fragment_count = 8;
    ok.total_len = 100;
    check(cdt_frame_validate(&ok), "frame_validate_ok_data", "合法 DATA 被拒");

    /* version / type / count / index / total_len 各拒绝分支 */
    cdt_frame_header_t bad = ok;
    bad.version = 2;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_version2", "version=2 未拒");

    bad = ok;
    bad.type = CDT_FRAME_TYPE_INVALID;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_type0", "type=0 未拒");
    bad.type = 4;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_type4", "type=4 未拒");

    bad = ok;
    bad.fragment_count = 0;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_count0", "count=0 未拒");
    bad.fragment_count = CDT_MAX_FRAGMENTS + 1u;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_count4097", "count=4097 未拒");

    bad = ok;
    bad.fragment_index = bad.fragment_count;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_index_oob", "index>=count 未拒");

    bad = ok;
    bad.type = CDT_FRAME_TYPE_ACK;
    bad.fragment_index = 1; /* ACK/NACK 必须 index=0 */
    check(!cdt_frame_validate(&bad), "frame_validate_reject_ack_index1", "ACK index=1 未拒");
    bad.fragment_index = 0;
    check(cdt_frame_validate(&bad), "frame_validate_ok_ack_index0", "ACK index=0 被误拒");

    bad = ok;
    bad.total_len = CDT_MAX_MESSAGE_LEN + 1u;
    check(!cdt_frame_validate(&bad), "frame_validate_reject_len_over", "total_len>16384 未拒");
    check(!cdt_frame_validate(NULL), "frame_validate_reject_null", "NULL 未拒");
}

/* ------------------------------------------------------------------ */
/* C. 发送侧分片器                                                     */
/* ------------------------------------------------------------------ */

static void test_fragmenter_unit(void)
{
    check(cdt_fragmenter_count(1, 4) == 1 &&
          cdt_fragmenter_count(4, 4) == 1 &&
          cdt_fragmenter_count(5, 4) == 2 &&
          cdt_fragmenter_count(16384, 4) == 4096 &&
          cdt_fragmenter_count(16384, 228) == 72,
          "frag_count_ceil_math", "ceil 计数不符");

    static uint8_t msg[CDT_MAX_MESSAGE_LEN + 1];
    fill_pattern(msg, sizeof(msg));

    cdt_fragmenter_t fg;
    check(!cdt_fragmenter_begin(&fg, NULL, 10, 1, 23), "frag_begin_reject_null",
          "NULL payload 未拒");
    check(!cdt_fragmenter_begin(&fg, msg, 0, 1, 23), "frag_begin_reject_len0",
          "total_len=0 未拒（DATA 不存在）");
    check(!cdt_fragmenter_begin(&fg, msg, CDT_MAX_MESSAGE_LEN + 1u, 1, 23),
          "frag_begin_reject_len_over", "total_len=16385 未拒");
    check(!cdt_fragmenter_begin(&fg, msg, 100, 1, 18), "frag_begin_reject_mtu18",
          "MTU18（容量≤0）未拒");
    /* MTU22→chunk3：16384B 需 5462 片 > 4096 上限 */
    check(!cdt_fragmenter_begin(&fg, msg, 16384, 1, 22), "frag_begin_reject_4096_over",
          "chunk=3 时 16384B 超 4096 片上限未拒");
    /* 位图 4096 片上限的正边界：MTU23、16384B 恰 4096 片，允许 */
    check(cdt_fragmenter_begin(&fg, msg, 16384, 1, 23) &&
          fg.fragment_count == CDT_MAX_FRAGMENTS,
          "frag_mtu23_exact_4096_ok", "MTU23/16KiB 恰 4096 片应允许");

    /* 片序列尺寸：10B @MTU23（chunk=4）→ 3 片 20/20/18，最后一片短片 */
    static const uint8_t ten[10] = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9'};
    check(cdt_fragmenter_begin(&fg, ten, 10, 7, 23), "frag_begin_10b_mtu23",
          "10B@MTU23 发起失败");
    check(fg.fragment_count == 3, "frag_count_10b_mtu23", "10B@chunk4 应为 3 片");
    size_t n0 = cdt_fragmenter_next(&fg, g_emit, 64);
    size_t n1 = cdt_fragmenter_next(&fg, g_emit + 64, 64);
    size_t n2 = cdt_fragmenter_next(&fg, g_emit + 128, 64);
    check(n0 == 20 && n1 == 20 && n2 == 18, "frag_frame_sizes_mtu23",
          "片尺寸应为 20/20/18（16B 头+载荷）");
    check(cdt_fragmenter_next(&fg, g_emit + 192, 64) == 0, "frag_done_returns_0",
          "done 后 next 应返回 0");

    /* 三片头除 fragment_index 外逐字段一致（§3.3 发送方 3） */
    cdt_frame_header_t h0, h1, h2;
    cdt_frame_decode(g_emit, &h0);
    cdt_frame_decode(g_emit + 64, &h1);
    cdt_frame_decode(g_emit + 128, &h2);
    int consistent =
        h0.version == 1 && h1.version == 1 && h2.version == 1 &&
        h0.type == CDT_FRAME_TYPE_DATA && h1.type == CDT_FRAME_TYPE_DATA &&
        h2.type == CDT_FRAME_TYPE_DATA &&
        h0.message_id == 7 && h1.message_id == 7 && h2.message_id == 7 &&
        h0.fragment_index == 0 && h1.fragment_index == 1 && h2.fragment_index == 2 &&
        h0.fragment_count == 3 && h1.fragment_count == 3 && h2.fragment_count == 3 &&
        h0.total_len == 10 && h1.total_len == 10 && h2.total_len == 10 &&
        h0.crc32 == h1.crc32 && h1.crc32 == h2.crc32 &&
        h0.crc32 == cdt_crc32(ten, 10);
    check(consistent, "frag_headers_consistent_except_index",
          "各片头应除 index 外逐字段一致且 CRC 为整包 CRC");
    check(memcmp(g_emit + 16, ten, 4) == 0 &&
          memcmp(g_emit + 64 + 16, ten + 4, 4) == 0 &&
          memcmp(g_emit + 128 + 16, ten + 8, 2) == 0,
          "frag_payload_slices", "片载荷偏移/长度与 §3.3 发送方 2 不符");
}

/* ------------------------------------------------------------------ */
/* D. 重组往返：MTU 矩阵 + 乱序 + 重复（§7.0 故障矩阵正路）             */
/* ------------------------------------------------------------------ */

static void test_roundtrips(void)
{
    static uint8_t msg[CDT_MAX_MESSAGE_LEN];
    cdt_reassembler_t rs;

    /* MTU23（chunk=4）16KiB 边界：4096 片逐字节往返（位图上限正例） */
    fill_pattern_16k(msg);
    rec_reset();
    check(cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0}), "rs_init_mtu23",
          "MTU23 初始化失败");
    rs.cbs = rec_cbs();
    check(build_frames(msg, 16384, 42, 23) == 4096, "build_16k_mtu23",
          "16KiB@MTU23 应产 4096 片");
    check(feed_frames(&rs, 0, g_emit_count, 1, 1, 1000, 1) == CDT_FEED_COMPLETED,
          "roundtrip_mtu23_16kib_feed", "末片喂入未完成重组");
    check(g_complete_count == 1 && g_completed_len == 16384 &&
          memcmp(g_completed, msg, 16384) == 0,
          "roundtrip_mtu23_16kib_bytes", "MTU23/16KiB 重组字节不一致");
    check(!cdt_reassembler_active(&rs), "context_cleared_after_complete",
          "交付后上下文应清空");

    /* MTU53（chunk=34）与 MTU247（chunk=228）矩阵 */
    const uint16_t mtus[] = {53, 247};
    for (int m = 0; m < 2; m++) {
        char detail[64];
        rec_reset();
        check(cdt_reassembler_init(&rs, mtus[m], &(cdt_reassembler_cbs_t){0}),
              "rs_init_matrix", "MTU 初始化失败");
        rs.cbs = rec_cbs();
        size_t len = (m == 0) ? 1000 : 16384;
        build_frames(msg, len, 1, mtus[m]);
        check(feed_frames(&rs, 0, g_emit_count, 1, 1, 0, 1) == CDT_FEED_COMPLETED,
              "roundtrip_mtu_feed", "喂入未完成");
        snprintf(detail, sizeof(detail), "MTU%u 重组字节不一致（大包矩阵）",
                 mtus[m]);
        check(g_completed_len == len && memcmp(g_completed, msg, len) == 0,
              "roundtrip_mtu_bytes", detail);
    }

    /* 单片消息（len < chunk）与恰整除（最后一片满容量） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 3, 1, 23);
    feed_frames(&rs, 0, g_emit_count, 1, 1, 0, 1);
    check(g_complete_count == 1 && g_completed_len == 3 &&
          memcmp(g_completed, msg, 3) == 0, "roundtrip_single_fragment",
          "单片消息往返失败");

    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 8, 1, 23); /* chunk=4 → 2 片且均满容量 */
    feed_frames(&rs, 0, g_emit_count, 1, 1, 0, 1);
    check(g_complete_count == 1 && g_completed_len == 8 &&
          memcmp(g_completed, msg, 8) == 0, "roundtrip_exact_chunk_multiple",
          "恰整除消息（末片满容量）往返失败");

    /* 乱序：首片先到锁定上下文，其后中段片逆序到达 → 最终重组正确
     * （§7.0；首片未到而中段先到属孤儿片，由 NACK 触发全量重发，
     * 语义见 reset 残留用例——不复活半包）。 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 100, 5, 23);
    check(feed_frame(&rs, g_emit + g_emit_off[0], g_emit_len[0], 0) == CDT_FEED_OK,
          "out_of_order_first_fragment", "首片喂入失败");
    check(feed_frames(&rs, 1, g_emit_count, -1, 1, 1, 1) == CDT_FEED_COMPLETED,
          "out_of_order_feed", "中段逆序喂入未完成");
    check(g_completed_len == 100 && memcmp(g_completed, msg, 100) == 0,
          "out_of_order_bytes", "乱序重组字节不一致");

    /* 重复片（每片喂 2 次）→ 忽略且结果正确（§7.0） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 100, 5, 23);
    check(feed_frames(&rs, 0, g_emit_count, 1, 2, 0, 1) == CDT_FEED_COMPLETED,
          "duplicate_feed_completed", "重复片喂入未完成");
    check(g_completed_len == 100 && memcmp(g_completed, msg, 100) == 0 &&
          g_reject_count == 0, "duplicate_bytes_ok_no_reject",
          "重复同数据片应被忽略（无拒绝、字节一致）");
}

/* ------------------------------------------------------------------ */
/* E. 拒绝路径（§7.0：一律拒绝整包、缓冲清空、不交付）                  */
/* ------------------------------------------------------------------ */

static void test_reject_paths(void)
{
    static uint8_t msg[4096];
    fill_pattern(msg, sizeof(msg));
    cdt_reassembler_t rs;

    /* 缺片：不到齐不交付；上下文保持直至 3s 进展超时兜底 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23); /* 4 片 */
    (void)feed_frames(&rs, 0, 3, 1, 1, 1000, 10); /* 只喂 0..2，末片进展 @1020 */
    check(g_complete_count == 0 && cdt_reassembler_active(&rs),
          "missing_fragment_no_delivery", "缺片不应交付且上下文应在");
    check(!cdt_reassembler_poll(&rs, 1020 + 2999) && cdt_reassembler_active(&rs),
          "missing_fragment_active_before_timeout", "2999ms 无进展不应丢弃");
    check(cdt_reassembler_poll(&rs, 1020 + 3000) && !cdt_reassembler_active(&rs) &&
          g_last_reason == CDT_REJECT_PROGRESS_TIMEOUT,
          "missing_fragment_progress_timeout", "缺片 3s 后应进展超时丢弃");

    /* 同 index 异数据 → 拒绝整包（DATA_MISMATCH）并清上下文 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 10);
    g_emit[g_emit_off[0] + CDT_FRAME_SIZE] ^= 0xFF; /* 篡改片 0 载荷首字节 */
    check(feed_frame(&rs, g_emit + g_emit_off[0], g_emit_len[0], 100) ==
              CDT_FEED_REJECTED &&
          g_last_reason == CDT_REJECT_DATA_MISMATCH && !cdt_reassembler_active(&rs),
          "reject_same_index_diff_data", "同 index 异数据未拒绝整包");
    g_emit[g_emit_off[0] + CDT_FRAME_SIZE] ^= 0xFF;

    /* CRC 坏：载荷在传输中损坏 → 齐片后整包 CRC 不符（CRC_MISMATCH） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    g_emit[g_emit_off[3] + 16 + 3] ^= 0x01; /* 只坏最后一片载荷 1 字节 */
    check(feed_frames(&rs, 0, g_emit_count, 1, 1, 0, 10) == CDT_FEED_REJECTED &&
          g_last_reason == CDT_REJECT_CRC_MISMATCH && g_complete_count == 0,
          "reject_bad_crc", "整包 CRC 不符未拒绝/误交付");

    /* 越界头：index == count（静态校验即拒，BAD_HEADER） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    {
        cdt_frame_header_t h;
        cdt_frame_decode(g_emit, &h);
        h.fragment_index = h.fragment_count; /* 越界 */
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_BAD_HEADER,
              "reject_index_oob_header", "越界 index 头未拒绝");
    }

    /* 几何矛盾：count 与 ceil(total_len/chunk) 不符（GEOMETRY） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    {
        cdt_frame_header_t h;
        h.version = CDT_FRAME_VERSION;
        h.type = CDT_FRAME_TYPE_DATA;
        h.message_id = 1;
        h.fragment_index = 0;
        h.fragment_count = 5; /* 16B/chunk4 应为 4 片，5 为矛盾头 */
        h.total_len = 16;
        h.crc32 = cdt_crc32(msg, 16);
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_GEOMETRY,
              "reject_geometry_count", "count 几何矛盾未拒绝");
    }

    /* 上下文矛盾：中途 total_len 变化（CONTEXT_MISMATCH） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 10);
    {
        cdt_frame_header_t h;
        cdt_frame_decode(g_emit + g_emit_off[1], &h);
        h.total_len = 15; /* 与已锁上下文矛盾 */
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 20) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_CONTEXT_MISMATCH &&
              !cdt_reassembler_active(&rs),
              "reject_context_len_mismatch", "中途 len 矛盾未拒绝整包");
    }

    /* 上下文矛盾：中途 crc32 变化 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 10);
    {
        cdt_frame_header_t h;
        cdt_frame_decode(g_emit + g_emit_off[1], &h);
        h.crc32 ^= 0x55;
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 20) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_CONTEXT_MISMATCH,
              "reject_context_crc_mismatch", "中途 crc 矛盾未拒绝整包");
    }

    /* 上下文矛盾：中途 message_id 变化（实现裁决：发送端一次仅 1 条在途，
     * 异 id 视为矛盾头；记录于 cdt_reassembler.h） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 10);
    {
        cdt_frame_header_t h;
        cdt_frame_decode(g_emit + g_emit_off[1], &h);
        h.message_id = 999;
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 20) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_CONTEXT_MISMATCH,
              "reject_context_id_mismatch", "中途异 message_id 未拒绝");
    }

    /* total_len=0 的 DATA：矛盾头（§3.3 发送方 1） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    {
        cdt_frame_header_t h;
        memset(&h, 0, sizeof(h));
        h.version = CDT_FRAME_VERSION;
        h.type = CDT_FRAME_TYPE_DATA;
        h.fragment_count = 1;
        h.total_len = 0;
        check(cdt_reassembler_feed(&rs, &h, NULL, 0, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_TOTAL_LEN_ZERO,
              "reject_total_len_zero", "total_len=0 未按矛盾头拒绝");
    }

    /* 孤儿片：无上下文时 index>0（含 reset 后残留）→ ORPHAN */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 16, 1, 23);
    check(feed_frame(&rs, g_emit + g_emit_off[2], g_emit_len[2], 0) ==
              CDT_FEED_REJECTED &&
          g_last_reason == CDT_REJECT_ORPHAN,
          "reject_orphan_index2", "无上下文 index>0 未拒绝");

    /* 片长矛盾：非最后片短片（BAD_FRAGMENT_SIZE） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    {
        cdt_frame_header_t h;
        h.version = CDT_FRAME_VERSION;
        h.type = CDT_FRAME_TYPE_DATA;
        h.message_id = 1;
        h.fragment_index = 0;
        h.fragment_count = 3;
        h.total_len = 10; /* chunk=4 → 期望片 0 长 4，喂 3 */
        h.crc32 = cdt_crc32(msg, 10);
        check(cdt_reassembler_feed(&rs, &h, msg, 3, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_BAD_FRAGMENT_SIZE,
              "reject_short_nonfinal_fragment", "非最后片短片未拒绝");
    }

    /* 非 DATA 帧不进重组器（BAD_HEADER）；version=2 同理 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    {
        cdt_frame_header_t h;
        h.version = CDT_FRAME_VERSION;
        h.type = CDT_FRAME_TYPE_ACK;
        h.message_id = 1;
        h.fragment_index = 0;
        h.fragment_count = 1;
        h.total_len = 4;
        h.crc32 = 0;
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_BAD_HEADER,
              "reject_ack_frame", "ACK 帧不应进重组器");
        h.type = CDT_FRAME_TYPE_DATA;
        h.version = 2;
        check(cdt_reassembler_feed(&rs, &h, msg, 4, 0) == CDT_FEED_REJECTED &&
              g_last_reason == CDT_REJECT_BAD_HEADER,
              "reject_version2_frame", "version=2 未拒绝");
    }

    /* 编程错误：NULL 头 → ARG_ERR，不回调、无副作用 */
    rec_reset();
    check(cdt_reassembler_feed(&rs, NULL, msg, 4, 0) == CDT_FEED_ARG_ERR &&
          g_reject_count == 0 && g_complete_count == 0,
          "arg_err_null_header", "NULL 头应返回 ARG_ERR 且不回调");
    check(cdt_reassembler_feed(&rs, &(cdt_frame_header_t){0}, NULL, 4, 0) ==
              CDT_FEED_ARG_ERR,
          "arg_err_null_payload", "NULL 载荷+len>0 应返回 ARG_ERR");
}

/* ------------------------------------------------------------------ */
/* F. 断连清空半包（P3.4 验收：半包不破坏 store；残留不复活）           */
/* ------------------------------------------------------------------ */

static void test_disconnect_clears_half_packet(void)
{
    static uint8_t msg[64];
    fill_pattern(msg, sizeof(msg));
    cdt_reassembler_t rs;

    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 64, 9, 23); /* 16 片 */
    (void)feed_frames(&rs, 0, 7, 1, 1, 0, 10); /* 断连前收到 7 片 */
    check(cdt_reassembler_active(&rs) && g_complete_count == 0,
          "half_packet_in_progress", "半包进行中应无交付");

    /* 断连：立刻清空（§3.3 接收方 6） */
    cdt_reassembler_reset(&rs);
    check(!cdt_reassembler_active(&rs), "reset_clears_context", "reset 后上下文应在");

    /* 残留不复活：旧消息剩余片（含 index=0 重放）不得完成重组 */
    check(feed_frame(&rs, g_emit + g_emit_off[7], g_emit_len[7], 100) ==
              CDT_FEED_REJECTED &&
          g_last_reason == CDT_REJECT_ORPHAN,
          "reset_residue_orphan", "reset 后旧片未按孤儿拒绝");
    check(feed_frame(&rs, g_emit + g_emit_off[0], g_emit_len[0], 101) == CDT_FEED_OK &&
          cdt_reassembler_active(&rs) && g_complete_count == 0,
          "reset_residue_index0_restarts_not_revives",
          "reset 后旧 index=0 只能作为全新消息首片，不得复活旧半包");
    /* 该"新消息"随后必须自己走完整流程才交付——补齐后按新 CRC 交付 */
    (void)feed_frames(&rs, 1, g_emit_count, 1, 1, 102, 10);
    check(g_complete_count == 1 && g_completed_len == 64 &&
          memcmp(g_completed, msg, 64) == 0,
          "fresh_full_resend_after_reset_completes",
          "重连后全量重发应完整重组（重连重发全量）");

    /* 破坏性验证：半包期间 store 侧无任何输入（交付次数=0 已断言），
     * 重组器永不以半包调用 on_complete（g_complete_count 全程为 0/1 已覆盖） */
}

/* ------------------------------------------------------------------ */
/* G. 超时（虚拟时钟，引擎判定；§3.3 接收方 4）                         */
/* ------------------------------------------------------------------ */

static void test_timeouts(void)
{
    static uint8_t msg[32];
    fill_pattern(msg, sizeof(msg));
    cdt_reassembler_t rs;

    /* 进展超时：3s 无新片 → 丢弃；2999ms 仍在 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 32, 1, 23); /* 8 片 */
    (void)feed_frames(&rs, 0, 2, 1, 1, 1000, 10); /* 末片进展 @1010 */
    check(!cdt_reassembler_poll(&rs, 1010 + 2999) && cdt_reassembler_active(&rs),
          "no_progress_timeout_at_2999", "2999ms 不应超时");
    check(cdt_reassembler_poll(&rs, 1010 + 3000) &&
          g_last_reason == CDT_REJECT_PROGRESS_TIMEOUT &&
          !cdt_reassembler_active(&rs) && g_complete_count == 0,
          "progress_timeout_at_3000", "3000ms 无进展应丢弃并回 NACK 依据");

    /* feed 自身先判超时：旧上下文丢弃后新消息 index=0 正常开新局 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 32, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 0);
    g_last_reason = CDT_REJECT_NONE;
    check(feed_frame(&rs, g_emit, g_emit_len[0], 3001) == CDT_FEED_OK &&
          g_last_reason == CDT_REJECT_PROGRESS_TIMEOUT && cdt_reassembler_active(&rs),
          "feed_reaps_stale_context", "喂入时应先收割超时旧上下文");
    (void)feed_frames(&rs, 1, g_emit_count, 1, 1, 3002, 1);
    check(g_complete_count == 1 && g_completed_len == 32,
          "new_message_after_timeout_completes", "超时后新消息应能完整重组");

    /* 整包超时：片间 1s 持续进展（不触发 3s 进展超时），30s 总闸生效。
     * 到期瞬间：先回调 TOTAL_TIMEOUT 丢弃上下文，随后到达的 index=30 片
     * 无上下文 → 再回调 ORPHAN（两个事件各可回一次 NACK，均触发全量重发）。 */
    static uint8_t msg128[128];
    fill_pattern(msg128, sizeof(msg128));
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg128, sizeof(msg128), 1, 23); /* 32 片，每 1s 一片 → 30s 总闸先于完成 */
    check(g_emit_count == 32, "total_timeout_frame_count", "128B@chunk4 应为 32 片");
    cdt_feed_result_t r = CDT_FEED_OK;
    for (size_t i = 0; i < g_emit_count; i++) {
        r = feed_frame(&rs, g_emit + g_emit_off[i], g_emit_len[i], (int64_t)i * 1000);
        if (r == CDT_FEED_REJECTED) {
            break;
        }
    }
    check(r == CDT_FEED_REJECTED && g_reject_log_n == 2 &&
          g_reject_log[0] == CDT_REJECT_TOTAL_TIMEOUT &&
          g_reject_log[1] == CDT_REJECT_ORPHAN && g_complete_count == 0,
          "total_timeout_30s_trickle", "持续进展下 30s 总闸未生效（或事件序列不符）");

    /* 重复片不计进展：dup 不刷新 3s 时钟（对照：新片刷新时钟） */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    build_frames(msg, 32, 1, 23);
    (void)feed_frames(&rs, 0, 1, 1, 1, 0, 0);    /* 片0 @0（真实进展） */
    (void)feed_frames(&rs, 0, 1, 1, 1, 2500, 0); /* 片0 重复 @2500（忽略） */
    check(!cdt_reassembler_poll(&rs, 2999) && cdt_reassembler_active(&rs),
          "dup_context_alive_at_2999", "@2999 距真实进展 2999ms，应仍存活");
    check(cdt_reassembler_poll(&rs, 3000) &&
          g_last_reason == CDT_REJECT_PROGRESS_TIMEOUT,
          "dup_does_not_refresh_progress",
          "@3000 即判超时 ⇔ 重复片未刷新 3s 时钟（若刷新须到 5500）");

    /* 对照组：@2500 到达的是新片（真实进展）→ 时钟被刷新 */
    rec_reset();
    cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0});
    rs.cbs = rec_cbs();
    (void)feed_frames(&rs, 0, 2, 1, 1, 0, 2500); /* 片0 @0、片1 @2500 */
    check(!cdt_reassembler_poll(&rs, 3000) && cdt_reassembler_active(&rs),
          "fresh_fragment_refreshes_progress", "新片应刷新进展时钟（@3000 仍活）");
    check(cdt_reassembler_poll(&rs, 5500) &&
          g_last_reason == CDT_REJECT_PROGRESS_TIMEOUT,
          "fresh_fragment_timeout_at_5500", "刷新后 @5500 应超时");
}

/* ------------------------------------------------------------------ */
/* H. ACK/NACK 决策辅助（§3.2 冻结填充 + §3.4 三值）                    */
/* ------------------------------------------------------------------ */

static void test_ack_nack(void)
{
    cdt_frame_header_t data;
    data.version = CDT_FRAME_VERSION;
    data.type = CDT_FRAME_TYPE_DATA;
    data.message_id = 0x11223344u;
    data.fragment_index = 6;
    data.fragment_count = 9;
    data.total_len = 1234;
    data.crc32 = 0xCAFEBABEu;

    uint8_t wire[CDT_FRAME_SIZE];
    cdt_frame_header_t h;

    /* applied → ACK */
    memset(wire, 0, sizeof(wire));
    cdt_build_ack_nack(&data, CDT_APPLY_APPLIED, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_ACK && h.version == CDT_FRAME_VERSION,
          "ack_applied_type", "applied 应产 ACK 帧");
    check(h.message_id == data.message_id && h.fragment_count == data.fragment_count &&
          h.total_len == data.total_len && h.crc32 == data.crc32 &&
          h.fragment_index == 0,
          "ack_metadata_backfill", "ACK 元数据回填与 §3.2 不符");
    check(cdt_frame_validate(&h), "ack_frame_valid", "ACK 帧未过静态校验");

    /* duplicate → ACK（同 epoch 同 seq 已应用，仍 ACK 不再渲染） */
    cdt_build_ack_nack(&data, CDT_APPLY_DUPLICATE, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_ACK, "duplicate_acks", "duplicate 应 ACK");

    /* rejected → NACK（触发重发全包） */
    cdt_build_ack_nack(&data, CDT_APPLY_REJECTED, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_NACK &&
          h.message_id == data.message_id && h.total_len == data.total_len &&
          h.crc32 == data.crc32 && h.fragment_index == 0 &&
          cdt_frame_validate(&h),
          "nack_rejected_frame", "rejected 应产合规 NACK 帧");

    /* 直接构造形式 */
    cdt_build_ack(&data, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_ACK, "build_ack_direct", "build_ack 类型错误");
    cdt_build_nack(&data, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_NACK, "build_nack_direct", "build_nack 类型错误");
}

/* ------------------------------------------------------------------ */
/* I. 单元级端到端：分片→重组→StateStore→三值 ACK/NACK                  */
/*    （Transport 不解析 JSON；store 调用仅在本测试代码）               */
/* ------------------------------------------------------------------ */

static const char E2E_SNAPSHOT[] =
    "{\"schema_version\":1,\"kind\":\"state\","
    "\"bridge_epoch\":\"e2e-000\",\"seq\":1,"
    "\"generated_at_ms\":null,"
    "\"source\":{\"kind\":\"mock\",\"connected\":false,"
    "\"stale\":true,\"last_event_at_ms\":null},"
    "\"selected_thread_id\":null,\"threads_total\":0,"
    "\"threads_truncated\":false,\"threads\":[],"
    "\"usage\":{\"available\":false,\"updated_at_ms\":null,"
    "\"windows_total\":0,\"windows_truncated\":false,\"windows\":[]}}";

static void test_e2e_fragment_reassemble_store_ack(void)
{
    static uint8_t msg[512];
    size_t len = strlen(E2E_SNAPSHOT);
    memcpy(msg, E2E_SNAPSHOT, len);

    cdt_reassembler_t rs;
    static cdt_state_store_t store;
    rec_reset();
    cdt_state_store_init(&store);
    check(cdt_reassembler_init(&rs, 23, &(cdt_reassembler_cbs_t){0}), "e2e_rs_init",
          "重组器初始化失败");
    rs.cbs = rec_cbs();

    build_frames(msg, len, 1, 23);
    check(feed_frames(&rs, 0, g_emit_count, 1, 1, 0, 1) == CDT_FEED_COMPLETED &&
          g_completed_len == len && memcmp(g_completed, msg, len) == 0,
          "e2e_reassembled_bytes", "端到端重组字节不一致");

    /* applied → ACK */
    cdt_parse_result_t r1 = cdt_state_store_apply(&store, g_completed, g_completed_len);
    check(r1 == CDT_PARSE_OK, "e2e_store_first_apply_applied",
          "首次应用应 CDT_PARSE_OK（applied→ACK）");
    if (r1 == CDT_PARSE_OK) {
        cdt_frame_header_t delivered;
        delivered.version = CDT_FRAME_VERSION;
        delivered.type = CDT_FRAME_TYPE_DATA;
        delivered.message_id = 1;
        delivered.fragment_index = 0;
        delivered.fragment_count = (uint16_t)g_emit_count;
        delivered.total_len = (uint16_t)len;
        delivered.crc32 = cdt_crc32(g_completed, g_completed_len);
        uint8_t wire[CDT_FRAME_SIZE];
        cdt_frame_header_t h;
        cdt_build_ack_nack(&delivered, CDT_APPLY_APPLIED, wire);
        cdt_frame_decode(wire, &h);
        check(h.type == CDT_FRAME_TYPE_ACK, "e2e_applied_ack", "applied 未产 ACK");
    }

    /* 重复投递（同 epoch 同 seq）→ IGNORED_STALE_SEQ → duplicate → ACK，
     * store 状态不被破坏 */
    cdt_parse_result_t r2 = cdt_state_store_apply(&store, g_completed, g_completed_len);
    check(r2 == CDT_PARSE_IGNORED_STALE_SEQ, "e2e_store_dup_ignored",
          "重复投递应 IGNORED_STALE_SEQ（duplicate→ACK 不再渲染）");
    const cdt_app_state_t *snap = cdt_state_store_snapshot(&store);
    check(snap != NULL && snap->seq == 1, "e2e_store_snapshot_intact",
          "重复投递后 store 快照应保持且未被破坏");
}

/* ------------------------------------------------------------------ */
/* CLI：loopback 接收端 / C 分片器产出（与 Python 互验）                */
/* ------------------------------------------------------------------ */

static uint8_t *read_whole_file(const char *path, size_t *out_len)
{
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return NULL;
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return NULL;
    }
    long sz = ftell(f);
    if (sz < 0 || (unsigned long)sz > 4u * 1024u * 1024u) {
        fclose(f);
        return NULL;
    }
    rewind(f);
    uint8_t *buf = malloc((size_t)sz + 1u);
    if (buf == NULL) {
        fclose(f);
        return NULL;
    }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        free(buf);
        fclose(f);
        return NULL;
    }
    fclose(f);
    buf[sz] = 0;
    *out_len = (size_t)sz;
    return buf;
}

static int write_whole_file(const char *path, const uint8_t *bytes, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (f == NULL) {
        return -1;
    }
    size_t n = fwrite(bytes, 1, len, f);
    fclose(f);
    return n == len ? 0 : -1;
}

static void put_u32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static uint32_t get_u32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

/* 帧文件容器：重复 [u32le 帧长][帧字节(16B 头+载荷)] 直到 EOF。
 * C 分片器与 bridge/transports/ble/fragmenter.py 共用此格式。 */

/* test_transport --emit-fragments ORIG MTU MSGID OUT */
static int mode_emit_fragments(int argc, char **argv)
{
    if (argc != 6) {
        fprintf(stderr, "usage: %s --emit-fragments ORIG MTU MSGID OUT\n", argv[0]);
        return 2;
    }
    size_t orig_len = 0;
    uint8_t *orig = read_whole_file(argv[2], &orig_len);
    if (orig == NULL) {
        fprintf(stderr, "FAIL: cannot read %s\n", argv[2]);
        return 1;
    }
    int mtu = atoi(argv[3]);
    uint32_t msg_id = (uint32_t)strtoul(argv[4], NULL, 10);
    cdt_fragmenter_t fg;
    if (!cdt_fragmenter_begin(&fg, orig, orig_len, msg_id, (uint16_t)mtu)) {
        fprintf(stderr, "FAIL: fragmenter_begin rejected (len=%zu mtu=%d)\n",
                orig_len, mtu);
        free(orig);
        return 1;
    }
    FILE *f = fopen(argv[5], "wb");
    if (f == NULL) {
        fprintf(stderr, "FAIL: cannot write %s\n", argv[5]);
        free(orig);
        return 1;
    }
    uint8_t frame[1024];
    size_t frames = 0;
    while (!cdt_fragmenter_done(&fg)) {
        size_t n = cdt_fragmenter_next(&fg, frame, sizeof(frame));
        if (n == 0) {
            fclose(f);
            free(orig);
            return 1;
        }
        uint8_t prefix[4];
        put_u32le(prefix, (uint32_t)n);
        fwrite(prefix, 1, 4, f);
        fwrite(frame, 1, n, f);
        frames++;
    }
    fclose(f);
    printf("emit-fragments: %zu frames (len=%zu mtu=%d id=%u) -> %s\n",
           frames, orig_len, mtu, msg_id, argv[5]);
    free(orig);
    return 0;
}

/* test_transport --loopback FRAGFILE ORIGFILE OUTFILE MTU
 * 接收端：喂重组器 → 逐字节比对原文件 → 写 OUTFILE → StateStore
 * applied/duplicate → ACK/NACK 决策辅助。 */
static int mode_loopback(int argc, char **argv)
{
    if (argc != 6) {
        fprintf(stderr, "usage: %s --loopback FRAGFILE ORIGFILE OUTFILE MTU\n",
                argv[0]);
        return 2;
    }
    const char *frag_path = argv[2];
    const char *orig_path = argv[3];
    const char *out_path = argv[4];
    uint16_t mtu = (uint16_t)atoi(argv[5]);

    int fails = 0;
    size_t frag_len = 0, orig_len = 0;
    uint8_t *frag = read_whole_file(frag_path, &frag_len);
    uint8_t *orig = read_whole_file(orig_path, &orig_len);
    if (frag == NULL || orig == NULL) {
        fprintf(stderr, "FAIL: cannot read %s or %s\n", frag_path, orig_path);
        free(frag);
        free(orig);
        return 1;
    }

    rec_reset();
    cdt_reassembler_t rs;
    cdt_reassembler_cbs_t cbs = rec_cbs();
    if (!cdt_reassembler_init(&rs, mtu, &cbs)) {
        fprintf(stderr, "FAIL: reassembler init mtu=%u\n", mtu);
        free(frag);
        free(orig);
        return 1;
    }

    size_t cursor = 0, frames = 0;
    int64_t now_ms = 0;
    cdt_frame_header_t last_hdr;
    memset(&last_hdr, 0, sizeof(last_hdr));
    int parse_err = 0;
    while (cursor + 4u <= frag_len) {
        uint32_t flen = get_u32le(frag + cursor);
        cursor += 4;
        if (flen < CDT_FRAME_SIZE || cursor + flen > frag_len) {
            parse_err = 1;
            break;
        }
        cdt_feed_result_t r = feed_frame(&rs, frag + cursor, flen, now_ms);
        cdt_frame_decode(frag + cursor, &last_hdr);
        if (r == CDT_FEED_REJECTED || r == CDT_FEED_ARG_ERR) {
            fprintf(stderr, "FAIL: frame #%zu rejected (reason=%d)\n",
                    frames, (int)g_last_reason);
            fails++;
        }
        cursor += flen;
        frames++;
        now_ms += 5; /* 虚拟链路：5ms/帧，远低于 3s 进展阈值 */
    }
    if (parse_err) {
        fprintf(stderr, "FAIL: malformed fragment file at frame #%zu\n", frames);
        fails++;
    }
    check(frames >= 1, "loopback_frame_count", "帧数为 0");

    /* 完整包恰好一次交付，且与原 JSON 逐字节一致（C 侧 memcmp） */
    check(g_complete_count == 1, "loopback_complete_once", "完整交付次数 != 1");
    check(g_completed_len == orig_len && memcmp(g_completed, orig, orig_len) == 0,
          "loopback_bytes_identical", "重组字节与原 JSON 不一致（C memcmp）");
    check(g_reject_count == 0, "loopback_no_rejects", "loopback 中出现拒绝");

    /* 独立证据：重组结果落盘，供 shell `cmp` 复核 */
    if (g_complete_count == 1) {
        if (write_whole_file(out_path, g_completed, g_completed_len) != 0) {
            fprintf(stderr, "FAIL: cannot write %s\n", out_path);
            fails++;
        }
    }

    /* StateStore：首次 applied → ACK；重复 → duplicate → ACK（store 不破坏） */
    static cdt_state_store_t store;
    cdt_state_store_init(&store);
    cdt_parse_result_t r1 = cdt_state_store_apply(&store, g_completed, g_completed_len);
    check(r1 == CDT_PARSE_OK, "loopback_store_applied",
          "StateStore 首次应用未通过（transport 只搬字节，store 由测试代码调用）");

    uint8_t wire[CDT_FRAME_SIZE];
    cdt_frame_header_t h;
    cdt_build_ack_nack(&last_hdr, CDT_APPLY_APPLIED, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_ACK && h.message_id == last_hdr.message_id &&
          h.fragment_count == last_hdr.fragment_count &&
          h.total_len == last_hdr.total_len && h.crc32 == last_hdr.crc32 &&
          h.fragment_index == 0 && cdt_frame_validate(&h),
          "loopback_ack_frame", "applied 的 ACK 帧与 §3.2 不符");

    cdt_parse_result_t r2 = cdt_state_store_apply(&store, g_completed, g_completed_len);
    check(r2 == CDT_PARSE_IGNORED_STALE_SEQ, "loopback_store_duplicate",
          "重复投递应 IGNORED_STALE_SEQ");
    cdt_build_ack_nack(&last_hdr, CDT_APPLY_DUPLICATE, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_ACK, "loopback_duplicate_ack", "duplicate 应 ACK");

    /* rejected → NACK 决策路径：上层对非法 JSON 拒绝（坏快照进新 store，
     * 结果非 OK 即三值中的 rejected → 触发重发全包） */
    static const uint8_t bad[] = "{\"kind\":\"state\"}";
    static cdt_state_store_t store_bad;
    cdt_state_store_init(&store_bad);
    cdt_parse_result_t r3 = cdt_state_store_apply(&store_bad, bad, sizeof(bad) - 1);
    check(r3 != CDT_PARSE_OK, "loopback_store_rejects_bad_json",
          "缺必填字段的 JSON 应被 store 拒绝（rejected 三值来源）");
    cdt_build_ack_nack(&last_hdr, CDT_APPLY_REJECTED, wire);
    cdt_frame_decode(wire, &h);
    check(h.type == CDT_FRAME_TYPE_NACK, "loopback_rejected_nack", "rejected 应 NACK");

    printf("loopback: %zu frames, %zu bytes reassembled (mtu=%u), %s\n",
           frames, g_completed_len, mtu,
           fails == 0 ? "ALL CHECKS PASS" : "HAS FAILURES");

    free(frag);
    free(orig);
    return fails == 0 && g_failures == 0 ? 0 : 1;
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "--loopback") == 0) {
        return mode_loopback(argc, argv);
    }
    if (argc >= 2 && strcmp(argv[1], "--emit-fragments") == 0) {
        return mode_emit_fragments(argc, argv);
    }
    if (argc >= 2) {
        fprintf(stderr, "usage: %s [--loopback F O OUT MTU | "
                        "--emit-fragments O MTU ID OUT]\n", argv[0]);
        return 2;
    }

    printf("== P3.4 transport 内核测试（C99 host，虚拟时钟） ==\n");
    test_crc_frozen_vectors();
    test_frame_codec();
    test_fragmenter_unit();
    test_roundtrips();
    test_reject_paths();
    test_disconnect_clears_half_packet();
    test_timeouts();
    test_ack_nack();
    test_e2e_fragment_reassemble_store_ack();

    printf("== 合计：%d PASS / %d FAIL ==\n", g_passes, g_failures);
    return g_failures == 0 ? 0 : 1;
}
