/*
 * cdt_reassembler.h — BLE DATA 分片接收侧重组器（P3.4，A4-B，host 阶段）
 *
 * 规则真源：docs/INTERFACES.md §7（冻结）与 protocol/transport.md §3.3（落地顺序）。
 * 范围红线：只搬字节——不解析 payload 内容、不产生业务状态；完整包经
 * on_complete 交付后，由调用方做 JSON/store，并以 applied/duplicate/rejected
 * 三值结果驱动 ACK/NACK（见 cdt_fragmenter.h 的决策辅助）。
 *
 * 冻结行为逐条（transport.md §3.3 接收方 1–6）：
 *   1. 首片锁定重组上下文（message_id/count/len/crc）；任一片与上下文矛盾
 *      （count/len/crc 不一致、index 越界、几何不符）→ 拒绝整包（清上下文）。
 *      实现裁决：不同 message_id 的 DATA 视为与上下文矛盾（发送端一次仅
 *      1 条在途，见 §3.3 发送方 4；此为矛盾头的一种，冻结文本未单列）。
 *   2. 同 index 同字节片 → 忽略（不计进展）；同 index 不同字节 → 拒绝整包。
 *   3. 缓冲上限 16384 + 4096 bit 位图；total_len==0 / >16384、
 *      fragment_count > 4096、几何（count != ceil(total_len/chunk)）→ 矛盾头。
 *   4. 3 秒无新片进展、或整条消息 30 秒未完成 → 丢弃上下文；超时判定由
 *      调用方喂 now_ms（引擎不用墙钟/真实睡眠）。
 *   5. 全部到齐 → cdt_crc32(重组字节) == 头中 crc32 ？交付：拒绝。
 *   6. cdt_reassembler_reset() 立刻清空半包与位图（断连）；reset 后旧片
 *      不复活（无上下文时 index>0 的 DATA 按孤儿片拒绝）。
 *
 * 纯逻辑、C99、零平台头；实例约 17KiB（16384 缓冲 + 512 位图），
 * 设备侧应静态分配。
 */
#ifndef CDT_REASSEMBLER_H
#define CDT_REASSEMBLER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cdt_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 拒绝原因（on_reject 第二参数；调用方可据此回 NACK/计数）。数值不跨版本稳定。 */
typedef enum {
    CDT_REJECT_NONE = 0,
    CDT_REJECT_BAD_ARG = 1,        /* 编程错误（NULL/缓冲不足），非协议拒绝 */
    CDT_REJECT_BAD_HEADER = 2,     /* version/type/静态校验不过（含 DATA index>=count） */
    CDT_REJECT_TOTAL_LEN_ZERO = 3, /* total_len==0：DATA 不存在（§3.3 发送方 1） */
    CDT_REJECT_TOTAL_LEN_OVER = 4, /* total_len>16384（防御；validate 已拦） */
    CDT_REJECT_COUNT_OVER = 5,     /* fragment_count>4096（防御；validate 已拦） */
    CDT_REJECT_GEOMETRY = 6,       /* count != ceil(total_len/chunk)：片容量矛盾 */
    CDT_REJECT_INDEX_OOB = 7,      /* index>=count */
    CDT_REJECT_BAD_FRAGMENT_SIZE = 8, /* 片长与固定容量/末片期望不符（越界头） */
    CDT_REJECT_CONTEXT_MISMATCH = 9,  /* 与已锁上下文 count/len/crc/id 矛盾 */
    CDT_REJECT_DATA_MISMATCH = 10,    /* 同 index 字节不同 */
    CDT_REJECT_ORPHAN = 11,           /* 无上下文时收到 index>0 片（残留不复活） */
    CDT_REJECT_CRC_MISMATCH = 12,     /* 齐片后整包 CRC 不符 */
    CDT_REJECT_PROGRESS_TIMEOUT = 13, /* 3s 无新片进展（§3.3 接收方 4） */
    CDT_REJECT_TOTAL_TIMEOUT = 14     /* 整包 30s 未完成（§3.3 接收方 4） */
} cdt_reject_reason_t;

/* 事件回调（均可为 NULL=忽略）。on_complete 交付重组后的完整字节
 * （指向引擎内部缓冲，仅在回调返回前有效——需要保留请调用方拷贝）。
 * on_reject 只用于协议拒绝；hdr 为"被拒/在途"帧头（可直接喂
 * cdt_build_ack_nack 造 NACK）。编程错误（BAD_ARG）不回调、不改变状态，
 * 仅以 CDT_FEED_ARG_ERR 返回值报告。 */
typedef struct {
    void *user;
    void (*on_complete)(void *user, const uint8_t *bytes, size_t len);
    void (*on_reject)(void *user, cdt_reject_reason_t reason,
                      const cdt_frame_header_t *hdr);
} cdt_reassembler_cbs_t;

/* 重组器实例（≈17KiB：16KiB 缓冲 + 512B 位图 + 上下文）。
 * 结构公开以便设备侧静态分配（同 cdt_state_store_t 风格）；
 * 字段属实现私有，调用方只走下面的函数接口，不得直接读写。 */
typedef struct cdt_reassembler {
    uint16_t chunk;               /* 固定片容量 = MTU−3−16，init 后不变 */
    cdt_reassembler_cbs_t cbs;

    /* 在途重组上下文（一次仅 1 条消息；active=false 时其余字段无效） */
    bool active;
    uint32_t message_id;
    uint16_t fragment_count;
    uint16_t total_len;
    uint32_t crc32;
    uint16_t received_count;
    int64_t first_ms;             /* 首片时间（30s 整包超时基准） */
    int64_t last_progress_ms;     /* 最近新片时间（3s 进展超时基准） */

    uint8_t buf[CDT_MAX_MESSAGE_LEN];      /* 16KiB 重组缓冲（冻结上限） */
    uint8_t bitmap[CDT_MAX_FRAGMENTS / 8]; /* 4096 片位图 = 512B */
} cdt_reassembler_t;

/* 初始化：mtu 为本连接协商 ATT_MTU；片容量 = CDT_FRAGMENT_CAPACITY(mtu)，
 * 必须 >0（MTU23 时为 4）。false = 容量非法（mtu<19）或参数 NULL。 */
bool cdt_reassembler_init(cdt_reassembler_t *rs, uint16_t mtu,
                          const cdt_reassembler_cbs_t *cbs);

/* 断连/重连：立刻清空半包、位图与超时上下文（§3.3 接收方 6）。 */
void cdt_reassembler_reset(cdt_reassembler_t *rs);

/* 喂入一片：hdr 为该 DATA 的 16 字节帧头（已解码），payload/payload_len
 * 为该片 ATT 写入的字节（不含帧头），now_ms 为调用方虚拟/单调毫秒。
 * 返回该片处理结果；同时经回调通知 complete/reject。 */
typedef enum {
    CDT_FEED_OK = 0,        /* 新片入位，或重复片被忽略 */
    CDT_FEED_COMPLETED = 1, /* 齐片且 CRC 通过，on_complete 已回调 */
    CDT_FEED_REJECTED = 2,  /* 拒绝整包（含超时丢弃），on_reject 已回调 */
    CDT_FEED_ARG_ERR = 3    /* 编程错误，引擎状态未变（BAD_ARG 除外仍回调） */
} cdt_feed_result_t;

cdt_feed_result_t cdt_reassembler_feed(cdt_reassembler_t *rs,
                                       const cdt_frame_header_t *hdr,
                                       const uint8_t *payload,
                                       size_t payload_len,
                                       int64_t now_ms);

/* 显式超时巡检（如定时器空闲时调用；feed 内部同样先判超时）。
 * 上下文因超时被丢弃时回调 on_reject 并返回 true。 */
bool cdt_reassembler_poll(cdt_reassembler_t *rs, int64_t now_ms);

/* 当前是否有在途重组上下文（测试/诊断用）。 */
bool cdt_reassembler_active(const cdt_reassembler_t *rs);

#ifdef __cplusplus
}
#endif

#endif /* CDT_REASSEMBLER_H */
