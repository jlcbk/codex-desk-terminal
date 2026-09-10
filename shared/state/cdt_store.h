/*
 * cdt_store.h — StateStore：整包替换 + seq/epoch 语义（P1.4，A0）
 *
 * 契约：INTERFACES §3 seq/bridge_epoch 行、§5 StateStore 行。
 *   apply(bytes,len) → CDT_PARSE_OK / IGNORED_STALE_SEQ / ERR_*：
 *   - 先完整解析到内部 scratch，任何 ERR 都不触碰当前状态（拒绝整包，保留
 *     最后合法状态——§3 末段）；解析通过后原子提交（memcpy）。
 *   - 同 epoch 且 seq <= last_seq → IGNORED_STALE_SEQ（重复/乱序丢弃，状态不变）。
 *   - 不同 epoch：接受并重置 seq 上下文（“仅在认证连接/重新握手中接受变化”的
 *     执行属 Transport/认证层，P3 落实；本 store 按新 epoch 全量同步处理）。
 *   - snapshot() 返回当前已应用状态；has_state=false 时返回 NULL。
 * 单线程边界：由单个解析任务拥有（§5 队列约定）；不加锁。
 */
#ifndef CDT_STORE_H
#define CDT_STORE_H

#include <stdbool.h>
#include <stddef.h>

#include "codex_state.h"

typedef struct {
    cdt_app_state_t current;
    cdt_app_state_t scratch; /* 解析暂存；sizeof≈2×17KiB，静态分配 */
    bool has_state;
    uint64_t last_seq;
    char epoch[CDT_MAX_BRIDGE_EPOCH_BYTES + 1];
} cdt_state_store_t;

void cdt_state_store_init(cdt_state_store_t *st);

cdt_parse_result_t cdt_state_store_apply(cdt_state_store_t *st, const void *bytes, size_t len);

const cdt_app_state_t *cdt_state_store_snapshot(const cdt_state_store_t *st);

#endif /* CDT_STORE_H */
