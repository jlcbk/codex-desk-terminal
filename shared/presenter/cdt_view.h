/*
 * cdt_view.h — ViewModel：AppState + DeviceRuntime 合成后的 UI 唯一输入
 *              （P2.1 NOW / P2.2+P2.3 全页面，A2）
 *
 * 契约：docs/INTERFACES.md §1（ViewModel 由 Presenter 在本地合并）、§4（Presenter
 * 优先级与计时语义、恢复顺序）、§6 NOW/AGENTS/PLAN/USAGE/LOW BATTERY 行
 * + 链路/陈旧提示位 + 静音位。
 *
 * 语义约定：
 *   - 本结构是纯数据：由 cdt_present() 填充（cdt_presenter.h），UI 只读。
 *   - 文本字段已由 presenter 按"显示列预算"截断（宽字符 2 列 / 窄字符 1 列的
 *     估算；像素级溢出兜底由 UI 层 LV_LABEL_LONG_DOT 承担）。截断按 UTF-8
 *     完整码点，绝不切断多字节序列（§6 换行/省略不能切断 UTF-8）。
 *   - 未知/缺失的"应有值"统一显示为 "--"（电压无效、无额度、无快照等）；
 *     "确实不存在的内容"（无计划、无 attention）用 *_present 布尔隐藏整行。
 *   - 时长仅在 source 与 link 均 fresh 时推进；陈旧冻结并置 time_frozen。
 *     格式：mm:ss，满 1 小时后 hh:mm:ss。
 *   - P2.2 新增：AGENTS 行（已按 §6 排序 + 分页计数）、PLAN 步骤（只数
 *     completed + 分页计数）、USAGE 逐窗口行（label 取自数据、reset 倒计时、
 *     context 独立行）。分页切片由导航状态（shared/ui/cdt_nav.h）选择，
 *     presenter 一次性给出全部可见行与页数。
 *
 * 纯类型，无外部依赖；不得 include LVGL/SDL/ESP 头。
 */
#ifndef CDT_VIEW_H
#define CDT_VIEW_H

#include <stdint.h>

#include "cdt_runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 显示列预算（presenter 截断用；1 列≈窄字符，2 列≈CJK/emoji 宽字符）---- */
#define CDT_VIEW_PROJECT_MAX_COLS 24  /* 标题栏左半（右侧留给电压） */
#define CDT_VIEW_ACTIVITY_MAX_COLS 38
#define CDT_VIEW_ATTENTION_MAX_COLS 38
#define CDT_VIEW_USAGE_MAX_COLS 42

/* 字节缓冲上限 = 最坏全宽字符 (cols/2)*3B + 截断符".." + NUL */
#define CDT_VIEW_PROJECT_BYTES 40
#define CDT_VIEW_ACTIVITY_BYTES 64
#define CDT_VIEW_ATTENTION_BYTES 64
#define CDT_VIEW_USAGE_BYTES 72

#define CDT_VIEW_STATUS_BYTES 16   /* "LOW BATTERY"=11B */
#define CDT_VIEW_PLAN_BYTES 24     /* "PLAN 65535/65535"=16B */
#define CDT_VIEW_ELAPSED_BYTES 16  /* hh:mm:ss（小时可 >99） */
#define CDT_VIEW_VOLTAGE_BYTES 8   /* "65.53V"=6B */

/* ---- P2.2：AGENTS 页（§6：排序、最多 4 行/页、总数/裁剪标记）---- */
#define CDT_VIEW_ROWS_PER_PAGE 4            /* AGENTS/PLAN 子页行数（P2 固定） */
#define CDT_VIEW_AGENTS_LABEL_BYTES 12      /* "THINKING"=8B */
#define CDT_VIEW_AGENTS_PROJECT_MAX_COLS 30
#define CDT_VIEW_AGENTS_PROJECT_BYTES 48    /* (30/2)*3B +".."+NUL */
#define CDT_VIEW_STEP_MAX_COLS 34
#define CDT_VIEW_STEP_BYTES 56              /* (34/2)*3B +".."+NUL */
#define CDT_VIEW_WIN_LABEL_MAX_COLS 18
#define CDT_VIEW_WIN_LABEL_BYTES 30         /* (18/2)*3B +".."+NUL */
#define CDT_VIEW_CONTEXT_BYTES 24           /* "CTX 100% (EST)"=14B */

/* ---- ZC5：NOW 信息条 / USAGE CONTEXT 行（效果图 1/5 对齐）---- */
#define CDT_VIEW_NOW_CTX_BYTES 20           /* "CTX 12345K"=11B */
#define CDT_VIEW_NOW_USAGE_BYTES 24         /* "5H 100%"=7B / "QU.. 100%"=9B */
#define CDT_VIEW_CONTEXT_LINE_BYTES 40      /* "CONTEXT 1234K / 1234K (100%)"=28B */

/* ---- ZC4：DETAILS 页（v1.2 增补字段；label 由 UI 静态绘制，缺值 "--"）---- */
#define CDT_VIEW_MODEL_MAX_COLS 24
#define CDT_VIEW_MODEL_BYTES 40             /* (24/2)*3B +".."+NUL */
#define CDT_VIEW_CTX_DETAIL_BYTES 40        /* "4194304K / 4194304K (100%)"=27B */
#define CDT_VIEW_TOKEN_TEXT_BYTES 12        /* "4194304K"=8B（uint32 饱和值） */

typedef struct {
    cdt_thread_state_t state;
    char state_label[CDT_VIEW_AGENTS_LABEL_BYTES]; /* ASCII 大写状态词 */
    char project[CDT_VIEW_AGENTS_PROJECT_BYTES];   /* 已截断；空 → "--" */
    char elapsed_text[CDT_VIEW_ELAPSED_BYTES]; /* ZC5：行尾右对齐时长（终态定格） */
    bool waiting;    /* needs_you：行首 "!" 等待提醒标记 */
    bool emphasized; /* needs_you / error 行强调 */
} cdt_agents_row_t;

typedef struct {
    char text[CDT_VIEW_STEP_BYTES]; /* 已截断（码点安全） */
    uint8_t status;                 /* cdt_step_status_t 值（uint8 压缩存储） */
} cdt_plan_step_row_t;

typedef struct {
    char label[CDT_VIEW_WIN_LABEL_BYTES]; /* 取自数据（不编造窗口名）；空 → "--" */
    bool pct_present;   /* false → UI 显示 "--"（不猜百分比） */
    uint8_t pct;        /* 0-100 取整 */
    uint16_t duration_mins; /* 实际窗口长度（分钟，来自数据） */
    bool reset_present;     /* resets_at_ms 为 null → false → UI 显示 "RST --" */
    int32_t reset_in_s;     /* 剩余秒；<0 = 已过 reset（不猜 0%，UI 显示 EXPIRED） */
} cdt_usage_row_t;

typedef struct {
    /* ---- 页面裁决（§4 优先级：本地低压 > 普通页；P2.3 完整低压页）---- */
    cdt_page_t page;         /* 经优先级裁决后的生效页 */
    bool low_battery_forced; /* true=电池 critical/sleep_prep 强制 LOW_BATTERY 页标记 */

    /* ---- 主状态词（§6 六状态之一，醒目展示）---- */
    cdt_thread_state_t status; /* INVALID=无业务状态（低压覆盖/尚无快照） */
    char status_label[CDT_VIEW_STATUS_BYTES]; /* ASCII 大写：NEEDS YOU / ERROR / … */
    bool status_emphasized;   /* needs_you（反白）/error（粗框）→ 黑白强调 */

    /* ---- 文本（presenter 已按列预算截断，UTF-8 码点安全）---- */
    char project[CDT_VIEW_PROJECT_BYTES];   /* 无任务/无快照 → "--" */
    char activity[CDT_VIEW_ACTIVITY_BYTES]; /* 空/无 → "--" */
    char attention[CDT_VIEW_ATTENTION_BYTES]; /* attention 为 null/计数 0 → 空串（UI 隐藏） */
    bool attention_present;

    /* ---- 时长（fresh 推进 / 陈旧冻结）---- */
    char elapsed_text[CDT_VIEW_ELAPSED_BYTES]; /* mm:ss / hh:mm:ss；无任务 "--" */
    char waiting_text[CDT_VIEW_ELAPSED_BYTES]; /* 同上；needs_you 时为等待时长 */
    bool time_frozen;        /* 链路或 source 陈旧：时长已冻结 */
    bool cancelled;          /* end_reason==cancelled → IDLE+已取消（§3/§6） */

    /* ---- 计划摘要 / 额度摘要 ---- */
    char plan_text[CDT_VIEW_PLAN_BYTES]; /* "PLAN 1/3"（completed/total） */
    bool plan_present;                   /* plan.total==0 → false（无计划，UI 隐藏行） */
    char usage_text[CDT_VIEW_USAGE_BYTES]; /* "SHORT WINDOW 42% +3"；无额度/无窗口 → "--" */

    /* ---- ZC5：NOW 仪表盘（效果图 1）----
     * PLAN mini 面板：复用 plan_steps 前 4 条（text+status）+ plan_completed /
     * plan_total 计数；plan_present==false → 整面板隐藏（无 plan 不留空框）。
     * 信息条两段：空串 = 隐藏该段，两段全空 → UI 整行隐藏。
     * WAITING 仅 needs_you 显示（working 等其他状态该位空白，ZC5 语义修复）：
     * waiting_present 由 presenter 裁决（cancelled 优先为 false）。 */
    bool waiting_present;                        /* 非取消且 needs_you → true */
    char now_ctx_text[CDT_VIEW_NOW_CTX_BYTES];   /* "CTX 68%"/"CTX 578K"；空=隐藏 */
    char now_usage_text[CDT_VIEW_NOW_USAGE_BYTES]; /* "5H 72%"；空=隐藏 */

    /* ---- ZC6：NOW 两态精修（效果图 1/2）----
     * alarm_mode（needs_you 专用警报布局，覆盖常规内容）：UI 据此切换为
     * 反白 NEEDS YOU 横幅 + "PERMISSION REQUIRED" 标签 + 命令独立边框盒
     * （内容复用 attention 摘要，已脱敏）+ "WAITING FOR APPROVAL <waiting_text>"
     * + "HOLD KEY = MUTE" 提示行，并隐藏 PLAN 面板/CTX 信息条/活动行；
     * cancelled（→ IDLE）与低压强制页不进警报布局。
     * status_dot：非警报态状态词左对齐实心圆点（unifont U+25CF；有效任务且
     * 非警报布局才亮，无任务/无快照/低压页不亮）。
     * elapsed_present：有任务 → true（UI 显示 "RUNNING FOR <elapsed_text>" 行；
     * 无任务/无快照时长为 "--" → 行隐藏）。 */
    bool alarm_mode;       /* needs_you 专用警报布局 */
    bool status_dot;       /* 状态词左对齐前缀实心圆点 */
    bool elapsed_present;  /* RUNNING FOR 行可见性（有任务） */

    /* ---- 电池（§7.1：UI 优先显示电压；百分比仅为估算）---- */
    char voltage_text[CDT_VIEW_VOLTAGE_BYTES]; /* "3.90V"；battery_valid=false → "--" */
    bool battery_valid;
    uint8_t usable_percent; /* 0-100 线性估算透传；battery_valid=false 时无意义 */

    /* ---- 链路/陈旧提示位（独立于业务状态，§4）---- */
    bool link_stale;         /* link stale 或上游 source.stale */
    bool link_disconnected;  /* 链路断开（提示强度高于 stale） */

    /* ---- 静音位（ACK=本地静音，绝不等于批准操作）---- */
    bool muted;

    /* ---- 其他透传（NOW 页可不用；P2.2 AGENTS 页使用）---- */
    uint16_t pending_count; /* attention.pending_count；attention null → 0 */
    uint16_t threads_total; /* 原始线程总数（> 可见数表示有裁剪） */

    /* ---- P2.2：AGENTS 页数据（已按 §6 排序；UI 经导航子页切片）---- */
    cdt_agents_row_t agents_rows[CDT_MAX_THREADS]; /* ≤8 行可见线程 */
    uint8_t agents_count;  /* 可见行数（=thread_count） */
    uint8_t agents_pages;  /* ceil(count/4)；无数据 → 1（单页 NO TASKS） */
    uint16_t agents_hidden; /* threads_total - thread_count（>0 → "还有 N 个"） */
    bool threads_truncated; /* 透传（与 agents_hidden 同源，UI 取其一） */

    /* ---- P2.2：PLAN 页数据（选中任务的计划；4 步/子页；只数 completed）---- */
    cdt_plan_step_row_t plan_steps[CDT_MAX_PLAN_STEPS]; /* 原始顺序 ≤8 步 */
    uint8_t plan_step_count; /* 可见步骤数 */
    uint16_t plan_total;     /* 原始总步数（=0 → UI 显示"暂无计划"） */
    bool plan_truncated;     /* 透传 */
    uint8_t plan_completed;  /* 只数 completed（in_progress/pending 不计） */
    uint8_t plan_pages;      /* ceil(step_count/4)；空计划 → 1 */

    /* ---- P2.2：USAGE 页数据（逐窗口行；label 来自数据）---- */
    cdt_usage_row_t usage_rows[CDT_MAX_USAGE_WINDOWS]; /* ≤4 窗口 */
    uint8_t usage_count;    /* 可见窗口数；available=false → 0（UI 显示 "--"） */
    char context_text[CDT_VIEW_CONTEXT_BYTES]; /* "CTX 43%" / "CTX --" */
    /* ZC5：windows 列表下方 CONTEXT 行（效果图 5）。capacity+used 已知 →
     * "CONTEXT 176K / 258K (68%)"（百分比取可信 used_percent，缺则由
     * used/capacity 计算）；仅 used → "CONTEXT 578K TOKENS"；全无 →
     * "CONTEXT --"（与页内其他缺值行风格一致）。 */
    char context_line[CDT_VIEW_CONTEXT_LINE_BYTES];

    /* ---- ZC4：DETAILS 页数据（v1.2 会话级字段；全部缺值 → "--"，不编造）---- */
    char model_text[CDT_VIEW_MODEL_BYTES]; /* 选中线程 model（列预算截断） */
    /* CONTEXT：capacity+used+percent 全知 → "176K / 258K (68%)"；仅 used →
     * "578K TOKENS"；全无 → "--"（累计 token 不冒充 context 百分比） */
    char context_detail_text[CDT_VIEW_CTX_DETAIL_BYTES];
    char tokens_in_text[CDT_VIEW_TOKEN_TEXT_BYTES];     /* K 格式化 / "--" */
    char tokens_out_text[CDT_VIEW_TOKEN_TEXT_BYTES];
    char tokens_cached_text[CDT_VIEW_TOKEN_TEXT_BYTES];
} cdt_view_t;

#ifdef __cplusplus
}
#endif

#endif /* CDT_VIEW_H */
