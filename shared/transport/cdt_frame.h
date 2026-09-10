/*
 * cdt_frame.h — Codex Desk Terminal BLE 传输帧头（16 字节，小端）共享 C 定义
 *
 * 由 P0.5 冻结（A4，2026-09-10），与 protocol/transport.md 一致；
 * 字段语义真源为 docs/INTERFACES.md §7（该表为冻结值，本文件只复述不改动）。
 * 变更须走 A0 契约变更流程并同步 protocol/transport.md、bridge/transports
 * 与 firmware/components/transport 三处实现。
 *
 * 范围与红线：
 *   - Transport 只搬字节：本头文件不引入 AppState/Telemetry 任何业务语义，
 *     不包含 JSON 字段、seq/epoch 等业务概念；payload 对本层完全不透明。
 *   - crc32 字段只检传输错误，不替代认证（INTERFACES §7）。
 *   - 纯类型与常量定义：C99、无外部依赖、不实现逻辑（实现属 P3.4）。
 *   - 所有多字节字段线上为小端；结构体仅用于内存表示，编解码须走
 *     cdt_frame_encode/cdt_frame_decode（声明见下，实现随 P3.4 交付），
 *     避免依赖主机字节序假设。
 */
#ifndef CDT_FRAME_H
#define CDT_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 帧常量（与 protocol/transport.md §3 一致）                          */
/* ------------------------------------------------------------------ */

#define CDT_FRAME_VERSION 1u  /* version 字段唯一合法值：帧版本 1 */

#define CDT_FRAME_SIZE 16u    /* 帧头总长：16 字节（冻结，不可改） */

/* type 字段（INTERFACES §7：DATA=1、ACK=2、NACK=3；0 保留作哨兵） */
#define CDT_FRAME_TYPE_INVALID 0u
#define CDT_FRAME_TYPE_DATA    1u
#define CDT_FRAME_TYPE_ACK     2u
#define CDT_FRAME_TYPE_NACK    3u

/* 消息与分片上限（INTERFACES §7：total_len ≤16384；最大片数 4096） */
#define CDT_MAX_MESSAGE_LEN 16384u
#define CDT_MAX_FRAGMENTS   4096u

/* 每次 ATT 写入的分片容量 = 协商 ATT_MTU − 3（ATT 写头）− 16（本帧头），
 * 要求 >0 方可工作（MTU23 时为 4 字节）。 */
#define CDT_ATT_OVERHEAD 3u
#define CDT_FRAGMENT_CAPACITY(mtu) ((mtu) - CDT_ATT_OVERHEAD - CDT_FRAME_SIZE)

/* 重组超时（INTERFACES §7：3 秒无进展丢弃；完整消息最长 30 秒） */
#define CDT_REASSEMBLY_PROGRESS_TIMEOUT_MS 3000u
#define CDT_REASSEMBLY_TOTAL_TIMEOUT_MS    30000u

/* 发送端限一次在途、最多 2 次重试；仍失败则重连/重新同步（§7） */
#define CDT_MAX_SEND_RETRIES 2u

/* ------------------------------------------------------------------ */
/* 帧头结构体（16 字节，#pragma pack(1)，字段顺序即线上字节顺序）       */
/* ------------------------------------------------------------------ */

#pragma pack(push, 1)

typedef struct {
    uint8_t version;         /* 偏移 0      ：CDT_FRAME_VERSION */
    uint8_t type;            /* 偏移 1      ：DATA/ACK/NACK */
    uint32_t message_id;     /* 偏移 2..5   ：小端；本次连接内递增，重连清空接收上下文 */
    uint16_t fragment_index; /* 偏移 6..7   ：小端；从 0 开始；ACK/NACK 固定 0 */
    uint16_t fragment_count; /* 偏移 8..9   ：小端；总片数，≥1；≤CDT_MAX_FRAGMENTS */
    uint16_t total_len;      /* 偏移 10..11 ：小端；完整 payload 字节数 ≤16384 */
    uint32_t crc32;          /* 偏移 12..15 ：小端；完整 payload 的 IEEE CRC32 */
} cdt_frame_header_t;

#pragma pack(pop)

/* 编译期尺寸断言：帧头不是 16 字节时在此报错（C99 无 _Static_assert）。 */
typedef char cdt_frame_header_size_check_[(sizeof(cdt_frame_header_t) == CDT_FRAME_SIZE) ? 1 : -1];

/* ------------------------------------------------------------------ */
/* CRC 参考实现声明                                                    */
/* ------------------------------------------------------------------ */

/*
 * 标准 IEEE CRC32：反射多项式 0xEDB88320，初值 0xFFFFFFFF，
 * 输出异或 0xFFFFFFFF——与 Python zlib.crc32 / binascii.crc32 逐位一致。
 * 冻结测试向量见 protocol/transport.md §4，复核脚本 scripts/gen_crc_vectors.py。
 * data 为 NULL 且 len>0 视为错误返回 0 之外的约定由实现定义；len==0 返回
 * 0x00000000（与 zlib.crc32(b"") 一致，对应冻结向量 V1）。
 */
uint32_t cdt_crc32(const uint8_t *data, size_t len);

/* ------------------------------------------------------------------ */
/* 编解码与静态校验（实现随 P3.4 交付；接口在此冻结）                   */
/* ------------------------------------------------------------------ */

/* 按小端把 hdr 编码为 16 字节线上格式；out 缓冲须 ≥CDT_FRAME_SIZE。 */
void cdt_frame_encode(const cdt_frame_header_t *hdr, uint8_t *out);

/* 从 16 字节线上格式解码（不校验语义，校验用 cdt_frame_validate）。 */
void cdt_frame_decode(const uint8_t *in, cdt_frame_header_t *hdr);

/* 静态合法性：version==1、type 合法、1≤fragment_count≤4096、
 * fragment_index<fragment_count 或 ACK/NACK 时为 0、total_len≤16384。
 * 只查帧自身一致性；分片容量（CDT_FRAGMENT_CAPACITY(mtu)）与 CRC、
 * 重组/去重/超时语义由 P3.4 的收发状态机执行。 */
bool cdt_frame_validate(const cdt_frame_header_t *hdr);

#ifdef __cplusplus
}
#endif

#endif /* CDT_FRAME_H */
