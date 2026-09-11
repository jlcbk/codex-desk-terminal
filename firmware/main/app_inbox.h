/*
 * app_inbox.h — WSS→app 任务收件槽（R4 跨任务所有权 + 优先提醒槽）
 *
 * 契约真源：docs/INTERFACES.md §5「解析任务将最新已验证快照交给UI任务，
 * 容量1的普通更新槽覆盖旧普通快照；NEEDS YOU/ERROR转换另保留一个有界优先
 * 事件槽并先呈现后续终态，避免慢链路吞掉提醒。超过预算时必须计数并重同步，
 * 不无限排队。」、§3 seq 行（同 epoch ≤ 已应用值丢弃——优先槽先出+store 的
 * seq 门卫共同保证最终显示停在最新状态）。
 * 审核条目：docs/ARCHITECTURE_REVIEW_2026-09-11.md R4（收件槽跨任务所有权、
 * 优先提醒槽）。
 *
 * 所有权模型（R4 修复核心）：
 *   - 生产者 = cdt_wss_client 的 on_text 回调（wss 任务上下文）：互斥锁短临界区
 *     内把完整消息 memcpy 进模块自有的普通/优先槽（二选一），更新占用长度。
 *   - 消费者 = app 主任务（唯一解析任务，StateStore 单线程边界）：互斥锁短临界区
 *     内把槽内容复制进**消费者独享缓冲**并释放槽；CRC/JSON 解析在锁外对独享
 *     缓冲进行——解析期间不持锁、不关中断（审核 R4 最小方向）。
 *   - 槽长度/占用/计数仅由锁保护，不依赖 volatile 语义。
 *
 * 优先槽语义（INTERFACES §5 冻结）：
 *   - app_inbox_is_priority() 判定为提醒转换（needs_you/error 存在）的消息进
 *     优先槽（容量 1）；其余进普通槽（容量 1，覆盖旧普通快照——dropped 计数
 *     语义保留）。优先槽被新提醒覆盖时同样计数（「超过预算时必须计数」）。
 *   - app_inbox_take() 优先槽先出：普通 DONE 不得顶掉尚未呈现的提醒转换；
 *     呈现（take→apply→render）后槽让位。同 epoch 内先出的旧 seq 快照若晚于
 *     新 seq 已应用，由 StateStore IGNORED_STALE_SEQ 拒绝，显示停在最新。
 *
 * 主机构建（tests/）：-DCDT_APP_INBOX_HOST 用 pthread 锁编译同一实现；
 * 固件构建用 FreeRTOS 互斥锁。交接/分类逻辑单源。
 */
#ifndef APP_INBOX_H
#define APP_INBOX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 下行聚合上限（cdt_wss_client.h 冻结 16384；本地复述避免固件外头依赖） */
#define APP_INBOX_MSG_MAX 16384u

/* 创建锁（固件：FreeRTOS 互斥锁）。必须在 on_text 可能被调用（cdt_wss_start）
 * 之前调用一次。 */
void app_inbox_init(void);

/* 生产者（wss 任务上下文；on_text 只交付完整消息）：按优先判定入槽。
 * 槽被未取走的旧消息覆盖时计入对应 dropped 计数。len>APP_INBOX_MSG_MAX 或
 * len==0 直接丢弃（聚合组件已保证上限，防御）。 */
void app_inbox_produce(const uint8_t *bytes, size_t len);

/* 消费者（唯一解析任务）：优先槽先出，其次普通槽；把整条消息复制进 dst
 * （消费者独享缓冲，cap ≥ APP_INBOX_MSG_MAX）后释放槽。无消息返回 false。
 * 复制在短临界区内完成；dst 供锁外解析，归调用方独有。 */
bool app_inbox_take(uint8_t *dst, size_t cap, size_t *len_out);

/* 普通槽 capacity-1 覆盖计数（原 s_inbox_dropped 语义，保留） */
uint32_t app_inbox_dropped_normal(void);
/* 优先槽预算计数：新提醒覆盖未呈现旧提醒的次数（§5 超预算必须计数） */
uint32_t app_inbox_dropped_priority(void);
/* 诊断：on_text 完整消息交付计数（原 main s_req_seq） */
uint32_t app_inbox_delivered(void);

/* 优先判定（纯函数，可测）：保守扫描本 Bridge 冻结编码（bridge/state/render.py
 * _encode：separators=(",",":") 紧凑形）的 `"state":"needs_you"` /
 * `"state":"error"` 字段形态，容忍键/值间空白变体。JSON 字符串值内的引号必为
 * `\"` 转义形态，无法匹配未转义字段形态——命中点只可能是真实键/值位（误报
 * 仅剩畸形重复键，方向安全：普通快照进优先槽最多被先呈现）。唯一发送方为
 * 本项目 Bridge；非紧凑对端由 §3/§5 的解析门卫兜底。 */
bool app_inbox_is_priority(const uint8_t *bytes, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* APP_INBOX_H */
