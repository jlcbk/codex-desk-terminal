/*
 * codex_state.h — Codex Desk Terminal AppState v1 共享 C 类型
 *
 * 由 P0.4 从 INTERFACES v1 草案生成，A0 于 2026-09-10 审查冻结为 FROZEN v1。
 * 变更须走主版本升级流程（INTERFACES §8）。A0 冻结裁决补记：
 *   - id 类字段（thread/turn/usage window）统一 <=128 字节。
 *   - 计数与总数上限 65535（与 uint16 存储对齐，避免合法 JSON 不可存储）。
 *   - context 与 usage 窗口的 used_percent 均为 0-100 或 null。
 *   - used_tokens/capacity_tokens 为非负整数或 null。
 * 真源：docs/INTERFACES.md §2（AppState 示例）与 §3（字段规则、上限与状态顺序）。
 * 配套：protocol/state.schema.json（同源约束；cdt-utf8-max-bytes 注解与本文件
 *       CDT_MAX_* 常量一一对应）。
 *
 * 范围与边界：
 *   - 纯类型定义：C99、无外部依赖、不实现解析逻辑（解析器属 P1.4）。
 *   - 只描述线上 AppState 的内存表示，不定义线上字节（§8：BLE/Wi-Fi 传同一
 *     JSON 字节，hash 一致；本类型不参与线上字节）。
 *   - 字符串上限按 UTF-8 字节数计（不含结尾 NUL）；char 数组长度 = 上限 + 1。
 *     §3 完整消息行：全部长度在分配内存前检查——定长数组下即写入前检查，
 *     超限整包拒绝（ERR_SIZE），绝不截断写入。
 *   - 可空（JSON null）表示约定：字符串/时间/数值字段用 * _present 标志；
 *     end_reason 用枚举值 CDT_END_REASON_NULL；attention 用 attention_present。
 *   - 拒绝整包语义：任何 ERR_* 结果都不得部分写入 StateStore，必须保留上一个
 *     已应用快照（§3 末段「类型错误、越界或缺必填字段拒绝整包，不能半应用」；
 *     §5 StateStore 行 apply_json→applied/ignored/error）。
 *   - schema_version==1 与 kind=="state" 在解码入口校验（ERR_VERSION /
 *     ERR_FIELD），不在本结构体占用存储：本结构体即 AppState。
 *   - 静态尺寸约 16–17 KiB（典型 32 位 ARM 编译，含对齐）；如超出目标内存预算，
 *     须由 A0 修订契约后调整，不得静默缩小数组。
 */
#ifndef CDT_CODEX_STATE_H
#define CDT_CODEX_STATE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* 协议常量（与 protocol/state.schema.json 一致）                       */
/* ------------------------------------------------------------------ */

#define CDT_STATE_SCHEMA_VERSION 1 /* §3：v1 仅接受整数 1 */

/* 字符串字节上限（UTF-8，不含 NUL）。来源 §3 字符串/bridge_epoch 行。 */
#define CDT_MAX_BRIDGE_EPOCH_BYTES 64  /* 1–64 字节 */
#define CDT_MAX_ID_BYTES 128           /* thread/turn/usage window id 统一上限（A0 冻结） */
#define CDT_MAX_TURN_ID_BYTES 128
#define CDT_MAX_PROJECT_BYTES 96
#define CDT_MAX_ACTIVITY_BYTES 192
#define CDT_MAX_ATTENTION_SUMMARY_BYTES 192
#define CDT_MAX_PLAN_TEXT_BYTES 128
#define CDT_MAX_USAGE_LABEL_BYTES 48
#define CDT_MAX_MODEL_BYTES 48        /* v1.2 增补：threads[].model（A0 2026-09-12） */

/* 数组上限与消息预算。来源 §3 threads/plan/usage/完整消息 行。 */
#define CDT_MAX_THREADS 8
#define CDT_MAX_PLAN_STEPS 8
#define CDT_MAX_USAGE_WINDOWS 4
#define CDT_STATE_JSON_MAX_BYTES 16384 /* 完整 UTF-8 JSON 上限 */
#define CDT_STATE_JSON_MAX_DEPTH 12    /* 嵌套深度上限（根对象计 1） */

/* seq 上界 2^53-1（§3 seq 行：同 epoch 严格递增安全整数）。 */
#define CDT_SEQ_MAX UINT64_C(9007199254740991)

/* ------------------------------------------------------------------ */
/* 枚举（全部带 invalid=0 哨兵；解码到 INVALID 视为 ERR_UNKNOWN_ENUM）   */
/* ------------------------------------------------------------------ */

/* §3 source.kind 行。zcode_observed = v1.1 增补（A0 2026-09-12）：ZCode 会话
 * 文件观察源；旧端 UNKNOWN_ENUM 拒包（fail-closed），bridge 与固件须成对部署。 */
typedef enum {
    CDT_SOURCE_INVALID = 0,
    CDT_SOURCE_MOCK = 1,
    CDT_SOURCE_CODEX_BRIDGE_OWNED = 2,
    CDT_SOURCE_CODEX_DESKTOP_OBSERVED = 3,
    CDT_SOURCE_ZCODE_OBSERVED = 4
} cdt_source_kind_t;

/* §3 state 行：idle / thinking / working / needs_you / done / error。
 * 未知枚举拒绝该快照，保留旧快照（ERR_UNKNOWN_ENUM）。 */
typedef enum {
    CDT_THREAD_STATE_INVALID = 0,
    CDT_THREAD_STATE_IDLE = 1,
    CDT_THREAD_STATE_THINKING = 2,
    CDT_THREAD_STATE_WORKING = 3,
    CDT_THREAD_STATE_NEEDS_YOU = 4,
    CDT_THREAD_STATE_DONE = 5,
    CDT_THREAD_STATE_ERROR = 6
} cdt_thread_state_t;

/* §3 end_reason 行：null / completed / failed / cancelled。
 * JSON null 是合法值，映射为 CDT_END_REASON_NULL；展示语义（cancelled 显示
 * idle 及取消说明）由 Presenter 承担。 */
typedef enum {
    CDT_END_REASON_INVALID = 0,
    CDT_END_REASON_NULL = 1, /* JSON null */
    CDT_END_REASON_COMPLETED = 2,
    CDT_END_REASON_FAILED = 3,
    CDT_END_REASON_CANCELLED = 4
} cdt_end_reason_t;

/* §3 plan 行：status 统一 pending/in_progress/completed。 */
typedef enum {
    CDT_STEP_STATUS_INVALID = 0,
    CDT_STEP_STATUS_PENDING = 1,
    CDT_STEP_STATUS_IN_PROGRESS = 2,
    CDT_STEP_STATUS_COMPLETED = 3
} cdt_step_status_t;

/* ------------------------------------------------------------------ */
/* 解析结果（§3 规则编号见各行注释；拒绝整包语义见文件头）               */
/* ------------------------------------------------------------------ */

typedef enum {
    CDT_PARSE_OK = 0,
    /* §3 seq 行：同 epoch <= 已应用值丢弃。不是格式错误；由 StateStore 依据
     * 已应用 seq 上下文判定（fixture 级校验无法判定，属运行时语义）。 */
    CDT_PARSE_IGNORED_STALE_SEQ = 1,
    /* §3 schema_version/kind 行：未知主版本拒绝并显示协议不兼容。 */
    CDT_PARSE_ERR_VERSION = 2,
    /* §3 字符串/threads/plan/usage/完整消息 行：任一字符串字节上限越界、
     * 数组超限或整包 >16384 字节。 */
    CDT_PARSE_ERR_SIZE = 3,
    /* §3 完整消息行：嵌套深度 >12。 */
    CDT_PARSE_ERR_DEPTH = 4,
    /* §3 末段 + threads/selected_thread_id/usage 行：缺必填、类型错误、数值
     * 越界、threads_total<windows/threads 长度、thread id 不唯一、
     * selected_thread_id 不指向已包含线程。 */
    CDT_PARSE_ERR_FIELD = 5,
    /* §3 字符串行（按 UTF-8 完整码点）+ §8 协议行：非法/截断 UTF-8 序列。 */
    CDT_PARSE_ERR_TRUNCATED_CP = 6,
    /* §3 state 行等：未知枚举值拒绝该快照，保留旧快照。 */
    CDT_PARSE_ERR_UNKNOWN_ENUM = 7
} cdt_parse_result_t;

/* ------------------------------------------------------------------ */
/* 结构体（与 state.schema.json 字段一一对应；字段顺序同 schema）        */
/* ------------------------------------------------------------------ */

/* §2 source 对象 */
typedef struct {
    cdt_source_kind_t kind;        /* INVALID=0 哨兵 */
    bool connected;                /* 上游状态，独立于设备无线连接 */
    bool stale;                    /* 上游失联保持最后值并标陈旧 */
    bool last_event_at_ms_present; /* false == JSON null */
    int64_t last_event_at_ms;      /* UTC Unix 毫秒 */
} cdt_source_t;

/* §2 attention 对象（整体可 null，见 cdt_thread_t.attention_present） */
typedef struct {
    uint16_t pending_count; /* 非负；上限 65535（A0 冻结，与 uint16 对齐） */
    char summary[CDT_MAX_ATTENTION_SUMMARY_BYTES + 1];
} cdt_attention_t;

/* §2 plan.steps[] 元素；status 以 uint8 存储（取值 cdt_step_status_t） */
typedef struct {
    char text[CDT_MAX_PLAN_TEXT_BYTES + 1];
    uint8_t status; /* cdt_step_status_t 值；uint8 压缩存储 */
} cdt_step_t;

/* §2 plan 对象：不为 null，但 steps 可为空数组（§3 末段） */
typedef struct {
    uint16_t total; /* 原始总数，可 > CDT_MAX_PLAN_STEPS（表示裁剪） */
    bool truncated;
    uint8_t step_count; /* <= CDT_MAX_PLAN_STEPS */
    cdt_step_t steps[CDT_MAX_PLAN_STEPS];
} cdt_plan_t;

/* §2 context 对象：三值均可 null（仅有可信来源才给值） */
typedef struct {
    bool used_tokens_present; /* false == JSON null */
    int64_t used_tokens;      /* 非负（A0 冻结） */
    bool capacity_tokens_present;
    int64_t capacity_tokens;
    bool used_percent_present; /* false == JSON null */
    double used_percent;       /* 0..100（A0 冻结确认适用）；P1.4 可评估定点表示，契约值仍为 0-100 */
} cdt_context_t;

/* §2 usage.windows[] 元素 */
typedef struct {
    char id[CDT_MAX_ID_BYTES + 1]; /* A0 冻结：窗口 id 与 thread/turn id 同上限 */
    char label[CDT_MAX_USAGE_LABEL_BYTES + 1];
    bool used_percent_present; /* false == JSON null */
    double used_percent;       /* 0..100 */
    uint16_t duration_mins;    /* 正整数分钟（>=1） */
    bool resets_at_ms_present; /* false == JSON null */
    int64_t resets_at_ms;      /* UTC Unix 毫秒 */
} cdt_usage_window_t;

/* §2 usage 对象 */
typedef struct {
    bool available;            /* 缺额度：available=false 且 window_count==0 */
    bool updated_at_ms_present; /* false == JSON null */
    int64_t updated_at_ms;
    uint16_t windows_total; /* 原始总数，可 > CDT_MAX_USAGE_WINDOWS；>= window_count */
    bool windows_truncated;
    uint8_t window_count; /* <= CDT_MAX_USAGE_WINDOWS */
    cdt_usage_window_t windows[CDT_MAX_USAGE_WINDOWS];
} cdt_usage_t;

/* §2 threads[] 元素；全部字段必填（§3 末段），可空项用 *_present/枚举 NULL */
typedef struct {
    char id[CDT_MAX_ID_BYTES + 1]; /* 线程数组内唯一 */
    bool turn_id_present;          /* false == JSON null */
    char turn_id[CDT_MAX_TURN_ID_BYTES + 1];
    char project[CDT_MAX_PROJECT_BYTES + 1];
    cdt_thread_state_t state;
    char activity[CDT_MAX_ACTIVITY_BYTES + 1];
    bool updated_at_ms_present; /* false == JSON null */
    int64_t updated_at_ms;
    uint64_t elapsed_ms;      /* 非负持续时间，不允许 null */
    uint64_t waiting_ms;      /* 非负持续时间，不允许 null */
    cdt_end_reason_t end_reason; /* CDT_END_REASON_NULL 表示 JSON null */
    bool attention_present;   /* false == JSON null（§3：attention 允许 null） */
    cdt_attention_t attention;
    cdt_plan_t plan; /* 不为 null；steps 可为空数组 */
    cdt_context_t context;
    /* ---- v1.2 可选增补（A0 2026-09-12，schema_version 仍为 1）----
     * 旧桥不发送：*_present=false，UI 显示 "--"。语义=会话级（跨 turn 不清零，
     * Bridge 侧 turn_started 不重置）；tokens 三值 >2^32 时解析器饱和到
     * UINT32_MAX（不拒包），负值/缺键仍属类型错误（ERR_FIELD，拒绝整包）。 */
    bool model_present;                /* false == 字段缺失或 JSON null */
    char model[CDT_MAX_MODEL_BYTES + 1];
    bool tokens_present;               /* false == tokens 对象缺失 */
    bool input_tokens_present;         /* false == JSON null */
    uint32_t input_tokens;             /* 饱和处理：>2^32-1 钳到 UINT32_MAX */
    bool output_tokens_present;
    uint32_t output_tokens;
    bool cached_tokens_present;
    uint32_t cached_tokens;
} cdt_thread_t;

/* §2 AppState 顶层对象（AppState 全量快照，§1） */
typedef struct {
    char bridge_epoch[CDT_MAX_BRIDGE_EPOCH_BYTES + 1]; /* 1–64 字节，每次启动新 ID */
    uint64_t seq; /* 0..CDT_SEQ_MAX，同 epoch 严格递增 */
    bool generated_at_ms_present; /* false == JSON null */
    int64_t generated_at_ms;      /* UTC Unix 毫秒 */
    cdt_source_t source;
    bool selected_thread_id_present; /* false == JSON null；非 null 时必须指向已包含线程 */
    char selected_thread_id[CDT_MAX_ID_BYTES + 1];
    uint16_t threads_total; /* 原始总数，可 > CDT_MAX_THREADS；>= thread_count */
    bool threads_truncated;
    uint8_t thread_count; /* <= CDT_MAX_THREADS */
    cdt_thread_t threads[CDT_MAX_THREADS];
    cdt_usage_t usage;
} cdt_app_state_t;

#ifdef __cplusplus
}
#endif

#endif /* CDT_CODEX_STATE_H */
