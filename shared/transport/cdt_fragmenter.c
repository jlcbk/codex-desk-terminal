/*
 * cdt_fragmenter.c — BLE DATA 发送侧分片器 + ACK/NACK 决策辅助实现
 * （P3.4，A4-B）。规则见 cdt_fragmenter.h（真源 INTERFACES §7 /
 * protocol/transport.md §3.2/§3.3，冻结值不改）。
 */
#include "cdt_fragmenter.h"

#include <string.h>

/* ------------------------------------------------------------------ */
/* 发送侧分片器                                                        */
/* ------------------------------------------------------------------ */

size_t cdt_fragmenter_count(size_t total_len, uint16_t chunk)
{
    if (chunk == 0u) {
        return 0; /* 调用方契约违例：无容量即无片 */
    }
    return (total_len + (size_t)chunk - 1u) / (size_t)chunk;
}

bool cdt_fragmenter_begin(cdt_fragmenter_t *fg, const uint8_t *payload,
                          size_t total_len, uint32_t message_id, uint16_t mtu)
{
    if (fg == NULL || payload == NULL) {
        return false;
    }
    /* total_len==0 不存在（AppState/Telemetry 均非空，§3.3 发送方 1） */
    if (total_len == 0u || total_len > (size_t)CDT_MAX_MESSAGE_LEN) {
        return false;
    }
    /* 片容量必须 >0：mtu < 3+16 时无有效容量（无符号下溢防御） */
    if (mtu < (uint16_t)(CDT_ATT_OVERHEAD + CDT_FRAME_SIZE)) {
        return false;
    }
    uint16_t chunk = (uint16_t)CDT_FRAGMENT_CAPACITY(mtu);
    if (chunk == 0u) {
        return false;
    }
    size_t count = cdt_fragmenter_count(total_len, chunk);
    if (count == 0u || count > (size_t)CDT_MAX_FRAGMENTS) {
        /* 4096 片上限（INTERFACES §7）：chunk<4 且大包时在此拒绝 */
        return false;
    }

    memset(fg, 0, sizeof(*fg));
    fg->payload = payload;
    fg->total_len = total_len;
    fg->message_id = message_id;
    fg->chunk = chunk;
    fg->fragment_count = (uint16_t)count;
    fg->crc32 = cdt_crc32(payload, total_len);
    fg->next_index = 0;
    return true;
}

bool cdt_fragmenter_done(const cdt_fragmenter_t *fg)
{
    return fg == NULL || fg->next_index >= fg->fragment_count;
}

size_t cdt_fragmenter_next(cdt_fragmenter_t *fg, uint8_t *out, size_t out_cap)
{
    if (fg == NULL || out == NULL || cdt_fragmenter_done(fg)) {
        return 0;
    }

    size_t offset = (size_t)fg->chunk * (size_t)fg->next_index;
    size_t remaining = fg->total_len - offset;
    size_t piece = remaining < (size_t)fg->chunk ? remaining : (size_t)fg->chunk;

    cdt_frame_header_t h;
    h.version = CDT_FRAME_VERSION;
    h.type = CDT_FRAME_TYPE_DATA;
    h.message_id = fg->message_id;
    h.fragment_index = fg->next_index;
    h.fragment_count = fg->fragment_count;
    h.total_len = (uint16_t)fg->total_len;
    h.crc32 = fg->crc32;

    if (out_cap < (size_t)CDT_FRAME_SIZE + piece) {
        return 0; /* 调用方缓冲不足：不消费片序号（可换大缓冲重试同片） */
    }
    cdt_frame_encode(&h, out);
    memcpy(out + CDT_FRAME_SIZE, fg->payload + offset, piece);

    fg->next_index++;
    return (size_t)CDT_FRAME_SIZE + piece;
}

/* ------------------------------------------------------------------ */
/* ACK/NACK 决策辅助（§3.2 冻结填充：回填被确认 DATA 元数据，index=0）  */
/* ------------------------------------------------------------------ */

static void build_control(const cdt_frame_header_t *confirmed_data,
                          uint8_t type, uint8_t *out)
{
    if (confirmed_data == NULL || out == NULL) {
        return;
    }
    cdt_frame_header_t h;
    h.version = CDT_FRAME_VERSION;
    h.type = type;
    h.message_id = confirmed_data->message_id;
    h.fragment_index = 0u;
    h.fragment_count = confirmed_data->fragment_count;
    h.total_len = confirmed_data->total_len;
    h.crc32 = confirmed_data->crc32;
    cdt_frame_encode(&h, out);
}

void cdt_build_ack_nack(const cdt_frame_header_t *confirmed_data,
                        cdt_apply_result_t result, uint8_t *out)
{
    build_control(confirmed_data,
                  result == CDT_APPLY_REJECTED ? CDT_FRAME_TYPE_NACK
                                               : CDT_FRAME_TYPE_ACK,
                  out);
}

void cdt_build_ack(const cdt_frame_header_t *confirmed_data, uint8_t *out)
{
    build_control(confirmed_data, CDT_FRAME_TYPE_ACK, out);
}

void cdt_build_nack(const cdt_frame_header_t *confirmed_data, uint8_t *out)
{
    build_control(confirmed_data, CDT_FRAME_TYPE_NACK, out);
}
