/*
 * cdt_fragmenter.h — BLE DATA 发送侧分片器 + ACK/NACK 决策辅助
 * （P3.4，A4-B，host 阶段）
 *
 * 规则真源：docs/INTERFACES.md §7（冻结）、protocol/transport.md §3.2/§3.3。
 * 红线：只搬字节——输入"完整 payload 字节 + MTU + message_id"，输出
 * 16 字节帧头+载荷的片序列；不解释 payload 内容。
 *
 * 分片规则（transport.md §3.3 发送方 1–2）：
 *   - 片容量 chunk = ATT_MTU − 3 − 16，必须 >0（MTU23 时为 4）。
 *   - fragment_count = ceil(total_len / chunk)（total_len==0 不存在）。
 *   - 第 i 片 payload = [i×chunk, min((i+1)×chunk, total_len))；
 *     只有最后一片允许短片。
 *   - 上限校验：total_len ≤16384、count ≤4096（越界拒绝发起分片）。
 *
 * 迭代式 API（零拷贝、无动态内存）：begin 校验并锁存输入 → 循环 next()
 * 逐片写出（帧头+载荷）→ done() 为止。所有片除 fragment_index 外逐字段
 * 一致（§3.3 发送方 3），crc32 为完整 payload 的 CRC32（init 时一次算好）。
 *
 * ACK/NACK 决策辅助（transport.md §3.2/§3.4 冻结填充）：输入上层
 * applied/duplicate/rejected 三值结果与被确认 DATA 的帧头，输出 16 字节
 * ACK/NACK 帧（回填 message_id/fragment_count/total_len/crc32，
 * fragment_index=0，无 payload）。
 *
 * 纯逻辑、C99、零平台头。
 */
#ifndef CDT_FRAGMENTER_H
#define CDT_FRAGMENTER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cdt_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 发送端句柄（约 40B，可栈分配；不拥有 payload）。 */
typedef struct {
    const uint8_t *payload;   /* 完整消息字节（调用方持有，生命周期覆盖迭代） */
    size_t total_len;         /* ≤ CDT_MAX_MESSAGE_LEN */
    uint32_t message_id;      /* 本次连接内递增（计数属调用方） */
    uint16_t chunk;           /* 固定片容量 = MTU−3−16 */
    uint16_t fragment_count;  /* ceil(total_len/chunk)，≥1 */
    uint32_t crc32;           /* 完整 payload 的 CRC32（begin 时一次计算） */
    uint16_t next_index;      /* 下一片序号 */
} cdt_fragmenter_t;

/* 一消息总片数 = ceil(total_len/chunk)（chunk 必须 >0，由调用方保证；
 * 供发送前预算/上限预检）。 */
size_t cdt_fragmenter_count(size_t total_len, uint16_t chunk);

/* 发起分片。校验：payload 非 NULL、1≤total_len≤16384、MTU≥19（chunk>0）、
 * count≤4096。false = 拒绝发起（调用方不应发送任何片）。 */
bool cdt_fragmenter_begin(cdt_fragmenter_t *fg, const uint8_t *payload,
                          size_t total_len, uint32_t message_id, uint16_t mtu);

/* 是否还有未发送的片。 */
bool cdt_fragmenter_done(const cdt_fragmenter_t *fg);

/* 生成下一片到 out（帧头+载荷，总长 = 16+片长 ≤ mtu；out_cap ≥ mtu 防
 * 越界）。返回写出字节数；0 = 参数错误或已发完（done 后再调）。 */
size_t cdt_fragmenter_next(cdt_fragmenter_t *fg, uint8_t *out, size_t out_cap);

/* ------------------------------------------------------------------ */
/* ACK/NACK 决策辅助（§3.4：Transport 只见三值信号，不读业务字段）      */
/* ------------------------------------------------------------------ */

/* 上层应用回调的三值结果（Store 层给出；语义见 transport.md §3.4 表）。 */
typedef enum {
    CDT_APPLY_APPLIED = 0,   /* 新快照接受   → ACK */
    CDT_APPLY_DUPLICATE = 1, /* 同 epoch 同 seq 已应用 → ACK（不再渲染） */
    CDT_APPLY_REJECTED = 2   /* JSON 非法/校验失败等   → NACK（触发重发全包） */
} cdt_apply_result_t;

/* 依三值结果生成 ACK(type=2) 或 NACK(type=3) 帧，写入 out（须 ≥
 * CDT_FRAME_SIZE）。回填被确认 DATA 的 message_id/fragment_count/
 * total_len/crc32；fragment_index=0；无 payload（帧共 16 字节）。 */
void cdt_build_ack_nack(const cdt_frame_header_t *confirmed_data,
                        cdt_apply_result_t result, uint8_t *out);

/* 直接构造 ACK / NACK 帧（同一冻结填充规则；决策辅助的底层形式）。 */
void cdt_build_ack(const cdt_frame_header_t *confirmed_data, uint8_t *out);
void cdt_build_nack(const cdt_frame_header_t *confirmed_data, uint8_t *out);

#ifdef __cplusplus
}
#endif

#endif /* CDT_FRAGMENTER_H */
