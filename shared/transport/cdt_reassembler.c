/*
 * cdt_reassembler.c — BLE DATA 分片接收侧重组器实现（P3.4，A4-B）。
 * 规则与裁决逐条注释见 cdt_reassembler.h（真源 INTERFACES §7 /
 * protocol/transport.md §3.3，冻结值不改）。
 */
#include "cdt_reassembler.h"

#include <string.h>

/* ------------------------------------------------------------------ */
/* 内部状态：结构定义公开于 cdt_reassembler.h（设备侧静态分配），
 * 字段语义与不变式见该头注释；本文件维护其操作逻辑。                  */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* 位图辅助（fragment_count ≤ 4096 由 cdt_frame_validate 保证，位图安全） */
/* ------------------------------------------------------------------ */

static bool bitmap_test(const uint8_t *bm, uint16_t index)
{
    return (bm[index >> 3] & (uint8_t)(1u << (index & 7u))) != 0u;
}

static void bitmap_set(uint8_t *bm, uint16_t index)
{
    bm[index >> 3] |= (uint8_t)(1u << (index & 7u));
}

static void ctx_clear(cdt_reassembler_t *rs)
{
    rs->active = false;
    rs->message_id = 0;
    rs->fragment_count = 0;
    rs->total_len = 0;
    rs->crc32 = 0;
    rs->received_count = 0;
    rs->first_ms = 0;
    rs->last_progress_ms = 0;
    memset(rs->bitmap, 0, sizeof(rs->bitmap));
    /* buf 不清零：重组按位图精确覆写，残留字节永不可达（重置语义见 .h） */
}

/* 从在途上下文重建一个 DATA 头（供超时丢弃时回 NACK 用；index=0）。 */
static cdt_frame_header_t ctx_header(const cdt_reassembler_t *rs)
{
    cdt_frame_header_t h;
    h.version = CDT_FRAME_VERSION;
    h.type = CDT_FRAME_TYPE_DATA;
    h.message_id = rs->message_id;
    h.fragment_index = 0;
    h.fragment_count = rs->fragment_count;
    h.total_len = rs->total_len;
    h.crc32 = rs->crc32;
    return h;
}

static void fire_reject(cdt_reassembler_t *rs, cdt_reject_reason_t reason,
                        const cdt_frame_header_t *hdr)
{
    if (rs->cbs.on_reject != NULL) {
        rs->cbs.on_reject(rs->cbs.user, reason, hdr);
    }
}

/* ------------------------------------------------------------------ */
/* 公共接口                                                            */
/* ------------------------------------------------------------------ */

bool cdt_reassembler_init(cdt_reassembler_t *rs, uint16_t mtu,
                          const cdt_reassembler_cbs_t *cbs)
{
    if (rs == NULL) {
        return false;
    }
    memset(rs, 0, sizeof(*rs));
    /* 无符号下溢防御：mtu < 19 时容量按负数处理（比较用更宽类型） */
    if (mtu < (uint16_t)(CDT_ATT_OVERHEAD + CDT_FRAME_SIZE)) {
        return false;
    }
    rs->chunk = (uint16_t)CDT_FRAGMENT_CAPACITY(mtu);
    if (rs->chunk == 0u) {
        return false;
    }
    if (cbs != NULL) {
        rs->cbs = *cbs;
    }
    ctx_clear(rs);
    return true;
}

void cdt_reassembler_reset(cdt_reassembler_t *rs)
{
    if (rs != NULL) {
        ctx_clear(rs);
    }
}

bool cdt_reassembler_active(const cdt_reassembler_t *rs)
{
    return rs != NULL && rs->active;
}

/* 超时判定（§3.3 接收方 4，达到阈值即超时；now_ms 由调用方喂入）。
 * 返回 true = 本调用丢弃了在途上下文。 */
static bool reject_if_timed_out(cdt_reassembler_t *rs, int64_t now_ms)
{
    if (!rs->active) {
        return false;
    }
    cdt_reject_reason_t reason = CDT_REJECT_NONE;
    if (now_ms - rs->last_progress_ms >= (int64_t)CDT_REASSEMBLY_PROGRESS_TIMEOUT_MS) {
        reason = CDT_REJECT_PROGRESS_TIMEOUT;
    } else if (now_ms - rs->first_ms >= (int64_t)CDT_REASSEMBLY_TOTAL_TIMEOUT_MS) {
        reason = CDT_REJECT_TOTAL_TIMEOUT;
    }
    if (reason == CDT_REJECT_NONE) {
        return false;
    }
    cdt_frame_header_t h = ctx_header(rs);
    ctx_clear(rs);
    fire_reject(rs, reason, &h);
    return true;
}

bool cdt_reassembler_poll(cdt_reassembler_t *rs, int64_t now_ms)
{
    if (rs == NULL) {
        return false;
    }
    return reject_if_timed_out(rs, now_ms);
}

cdt_feed_result_t cdt_reassembler_feed(cdt_reassembler_t *rs,
                                       const cdt_frame_header_t *hdr,
                                       const uint8_t *payload,
                                       size_t payload_len,
                                       int64_t now_ms)
{
    if (rs == NULL || hdr == NULL ||
        (payload == NULL && payload_len > 0u)) {
        return CDT_FEED_ARG_ERR; /* 编程错误：不回调、状态不变 */
    }

    /* §3.3 接收方 4：任何新片到来前先判在途上下文超时 */
    (void)reject_if_timed_out(rs, now_ms);

    /* 静态帧校验 + 仅接受 DATA（ACK/NACK 不进重组器） */
    if (!cdt_frame_validate(hdr) || hdr->type != CDT_FRAME_TYPE_DATA) {
        fire_reject(rs, CDT_REJECT_BAD_HEADER, hdr);
        return CDT_FEED_REJECTED;
    }
    /* 矛盾头尺寸守门（validate 已拦大部分，这里按 §3.3 接收方 3 显式复述） */
    if (hdr->total_len == 0u) {
        fire_reject(rs, CDT_REJECT_TOTAL_LEN_ZERO, hdr);
        return CDT_FEED_REJECTED;
    }
    if (hdr->total_len > CDT_MAX_MESSAGE_LEN) {
        fire_reject(rs, CDT_REJECT_TOTAL_LEN_OVER, hdr);
        return CDT_FEED_REJECTED;
    }
    if (hdr->fragment_count > CDT_MAX_FRAGMENTS) {
        fire_reject(rs, CDT_REJECT_COUNT_OVER, hdr);
        return CDT_FEED_REJECTED;
    }

    /* 固定片容量几何：count 必须 == ceil(total_len / chunk)，否则发送端
     * 与本端 MTU 视图不一致，offset 体系失效 → 矛盾头拒绝 */
    {
        uint16_t expect_count =
            (uint16_t)((hdr->total_len + rs->chunk - 1u) / rs->chunk);
        if (hdr->fragment_count != expect_count) {
            fire_reject(rs, CDT_REJECT_GEOMETRY, hdr);
            return CDT_FEED_REJECTED;
        }
    }

    if (hdr->fragment_index >= hdr->fragment_count) {
        fire_reject(rs, CDT_REJECT_INDEX_OOB, hdr);
        return CDT_FEED_REJECTED;
    }

    /* 片长 = 固定容量；只有最后一片允许短片，且长度由头唯一确定
     * （§3.3 发送方 2：越界头/矛盾片长都在此拒绝） */
    {
        size_t expected_len;
        if (hdr->fragment_index + 1u < hdr->fragment_count) {
            expected_len = rs->chunk;
        } else {
            expected_len = (size_t)hdr->total_len -
                           (size_t)rs->chunk * (size_t)(hdr->fragment_count - 1u);
        }
        if (payload_len != expected_len) {
            fire_reject(rs, CDT_REJECT_BAD_FRAGMENT_SIZE, hdr);
            return CDT_FEED_REJECTED;
        }
    }

    /* §3.3 接收方 1：首片锁定上下文；后续片与上下文逐字段比对 */
    if (!rs->active) {
        if (hdr->fragment_index != 0u) {
            /* 断连/reset 后旧半包残留不复活（§3.3 接收方 6） */
            fire_reject(rs, CDT_REJECT_ORPHAN, hdr);
            return CDT_FEED_REJECTED;
        }
        rs->active = true;
        rs->message_id = hdr->message_id;
        rs->fragment_count = hdr->fragment_count;
        rs->total_len = hdr->total_len;
        rs->crc32 = hdr->crc32;
        rs->received_count = 0;
        rs->first_ms = now_ms;
        rs->last_progress_ms = now_ms;
    } else if (hdr->message_id != rs->message_id ||
               hdr->fragment_count != rs->fragment_count ||
               hdr->total_len != rs->total_len ||
               hdr->crc32 != rs->crc32) {
        /* 实现裁决：id 不一致同样按矛盾头拒绝（发送端一次仅 1 条在途） */
        fire_reject(rs, CDT_REJECT_CONTEXT_MISMATCH, hdr);
        ctx_clear(rs);
        return CDT_FEED_REJECTED;
    }

    /* §3.3 接收方 2：同 index 同数据忽略（不计进展），异数据拒绝整包 */
    if (bitmap_test(rs->bitmap, hdr->fragment_index)) {
        size_t off = (size_t)rs->chunk * (size_t)hdr->fragment_index;
        if (memcmp(rs->buf + off, payload, payload_len) == 0) {
            return CDT_FEED_OK; /* 忽略：不更新 last_progress */
        }
        fire_reject(rs, CDT_REJECT_DATA_MISMATCH, hdr);
        ctx_clear(rs);
        return CDT_FEED_REJECTED;
    }

    /* 新片入位（offset = index × 固定片容量，乱序安全） */
    memcpy(rs->buf + (size_t)rs->chunk * (size_t)hdr->fragment_index,
           payload, payload_len);
    bitmap_set(rs->bitmap, hdr->fragment_index);
    rs->received_count++;
    rs->last_progress_ms = now_ms;

    /* §3.3 接收方 5：齐片 → CRC → 交付上层（JSON/store 属调用方） */
    if (rs->received_count == rs->fragment_count) {
        uint32_t computed = cdt_crc32(rs->buf, rs->total_len);
        cdt_frame_header_t h = ctx_header(rs);
        ctx_clear(rs);
        if (computed == h.crc32) {
            if (rs->cbs.on_complete != NULL) {
                rs->cbs.on_complete(rs->cbs.user, rs->buf, h.total_len);
            }
            return CDT_FEED_COMPLETED;
        }
        fire_reject(rs, CDT_REJECT_CRC_MISMATCH, &h);
        return CDT_FEED_REJECTED;
    }

    return CDT_FEED_OK;
}
