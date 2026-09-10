/*
 * cdt_crc32.c — cdt_frame.h（P0.5 冻结）所声明接口的 P3.4 实现（A4-B，host 阶段）。
 *
 * 本翻译单元实现 cdt_frame.h 中"实现随 P3.4 交付"的全部原语：
 *   1) cdt_crc32            — 标准 IEEE CRC32（反射多项式 0xEDB88320，
 *                             初值/终值异或 0xFFFFFFFF），与 Python
 *                             zlib.crc32 / binascii.crc32 逐位一致；
 *                             锚点 = protocol/transport.md §4 的 4 个冻结向量
 *                             （V1 empty / V2 short_json / V3 pattern_16k /
 *                             V4 fox_reference，见 tests/transport/ble）。
 *   2) cdt_frame_encode     — 头结构 → 16 字节小端线上格式。
 *   3) cdt_frame_decode     — 16 字节线上格式 → 头结构（不做语义校验）。
 *   4) cdt_frame_validate   — 帧自身静态合法性（语义见头文件注释）。
 *
 * 编解码实现选择显式移位/字节装配而非直接结构体拷贝，避免依赖主机字节序
 * 与 #pragma pack 的内存布局假设（线上字节偏移冻结为 transport.md §3.1：
 * version=0、type=1、message_id=2..5、fragment_index=6..7、fragment_count=8..9、
 * total_len=10..11、crc32=12..15，多字节一律小端）。
 *
 * 红线：纯逻辑、C99、零平台头（禁 esp_/SDL/lvgl/freertos）、不引入任何
 * AppState/Telemetry 业务语义；Transport 只搬字节。
 */
#include "cdt_frame.h"

/* ------------------------------------------------------------------ */
/* 1. 标准 IEEE CRC32                                                  */
/* ------------------------------------------------------------------ */

uint32_t cdt_crc32(const uint8_t *data, size_t len)
{
    /* len==0 → 0x00000000（0xFFFFFFFF 初值经 8*0 次迭代后异或 0xFFFFFFFF，
     * 与 zlib.crc32(b"") 一致，对应冻结向量 V1）。data 为 NULL 且 len>0
     * 视为编程错误，按头文件约定返回非 0 哨兵值（非冻结向量之一）。 */
    if (data == NULL && len > 0u) {
        return 0xDEADBEEFu;
    }

    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint32_t)data[i];
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 1u) {
                crc = (crc >> 1) ^ 0xEDB88320u;
            } else {
                crc >>= 1;
            }
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

/* ------------------------------------------------------------------ */
/* 2/3. 帧头编解码（显式小端装配）                                      */
/* ------------------------------------------------------------------ */

void cdt_frame_encode(const cdt_frame_header_t *hdr, uint8_t *out)
{
    if (hdr == NULL || out == NULL) {
        return; /* 编程错误：调用方保证非空（纯 C99 无异常可抛） */
    }
    out[0] = hdr->version;
    out[1] = hdr->type;
    out[2] = (uint8_t)(hdr->message_id & 0xFFu);
    out[3] = (uint8_t)((hdr->message_id >> 8) & 0xFFu);
    out[4] = (uint8_t)((hdr->message_id >> 16) & 0xFFu);
    out[5] = (uint8_t)((hdr->message_id >> 24) & 0xFFu);
    out[6] = (uint8_t)(hdr->fragment_index & 0xFFu);
    out[7] = (uint8_t)((hdr->fragment_index >> 8) & 0xFFu);
    out[8] = (uint8_t)(hdr->fragment_count & 0xFFu);
    out[9] = (uint8_t)((hdr->fragment_count >> 8) & 0xFFu);
    out[10] = (uint8_t)(hdr->total_len & 0xFFu);
    out[11] = (uint8_t)((hdr->total_len >> 8) & 0xFFu);
    out[12] = (uint8_t)(hdr->crc32 & 0xFFu);
    out[13] = (uint8_t)((hdr->crc32 >> 8) & 0xFFu);
    out[14] = (uint8_t)((hdr->crc32 >> 16) & 0xFFu);
    out[15] = (uint8_t)((hdr->crc32 >> 24) & 0xFFu);
}

void cdt_frame_decode(const uint8_t *in, cdt_frame_header_t *hdr)
{
    if (in == NULL || hdr == NULL) {
        return;
    }
    hdr->version = in[0];
    hdr->type = in[1];
    hdr->message_id = (uint32_t)in[2]
                    | ((uint32_t)in[3] << 8)
                    | ((uint32_t)in[4] << 16)
                    | ((uint32_t)in[5] << 24);
    hdr->fragment_index = (uint16_t)((uint16_t)in[6] | ((uint16_t)in[7] << 8));
    hdr->fragment_count = (uint16_t)((uint16_t)in[8] | ((uint16_t)in[9] << 8));
    hdr->total_len = (uint16_t)((uint16_t)in[10] | ((uint16_t)in[11] << 8));
    hdr->crc32 = (uint32_t)in[12]
               | ((uint32_t)in[13] << 8)
               | ((uint32_t)in[14] << 16)
               | ((uint32_t)in[15] << 24);
}

/* ------------------------------------------------------------------ */
/* 4. 静态合法性（只查帧自身一致性，语义见 cdt_frame.h 注释）           */
/* ------------------------------------------------------------------ */

bool cdt_frame_validate(const cdt_frame_header_t *hdr)
{
    if (hdr == NULL) {
        return false;
    }
    if (hdr->version != CDT_FRAME_VERSION) {
        return false;
    }
    if (hdr->type != CDT_FRAME_TYPE_DATA &&
        hdr->type != CDT_FRAME_TYPE_ACK &&
        hdr->type != CDT_FRAME_TYPE_NACK) {
        return false;
    }
    if (hdr->fragment_count < 1u || hdr->fragment_count > CDT_MAX_FRAGMENTS) {
        return false;
    }
    if (hdr->total_len > CDT_MAX_MESSAGE_LEN) {
        return false;
    }
    if (hdr->type == CDT_FRAME_TYPE_DATA) {
        if (hdr->fragment_index >= hdr->fragment_count) {
            return false;
        }
    } else {
        /* ACK/NACK：fragment_index 固定 0（transport.md §3.2 冻结） */
        if (hdr->fragment_index != 0u) {
            return false;
        }
    }
    return true;
}
