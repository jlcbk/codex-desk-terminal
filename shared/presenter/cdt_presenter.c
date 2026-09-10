/*
 * cdt_presenter.c — Presenter 纯转换实现（P2.1，A2）
 *
 * 规则真源：docs/INTERFACES.md §4（Presenter 优先级、fresh 计时）、§6 NOW 行、
 * §7.1（电压显示）。本文件必须保持无 LVGL/SDL/ESP 依赖（build_presenter_tests.sh
 * 有 grep 门禁断言）。
 */
#include <stdio.h>
#include <string.h>

#include "cdt_presenter.h"

/* ================================================================== */
/* UTF-8 工具：码点解码 / 显示宽估算 / 按列预算截断（码点安全）          */
/* ================================================================== */

#define CP_INVALID 0xFFFFFFFFu

/* 解码 1 个 UTF-8 码点；返回消耗字节数（非法序列返回 1 并给出 CP_INVALID）。*/
static size_t utf8_decode(const uint8_t *s, size_t avail, uint32_t *cp_out)
{
    uint8_t b0 = s[0];
    uint32_t cp;
    size_t n, i;

    if (b0 < 0x80u) {
        *cp_out = b0;
        return 1;
    }
    if ((b0 & 0xE0u) == 0xC0u) { n = 2; cp = b0 & 0x1Fu; }
    else if ((b0 & 0xF0u) == 0xE0u) { n = 3; cp = b0 & 0x0Fu; }
    else if ((b0 & 0xF8u) == 0xF0u) { n = 4; cp = b0 & 0x07u; }
    else { *cp_out = CP_INVALID; return 1; }

    if (avail < n) { *cp_out = CP_INVALID; return 1; }
    for (i = 1; i < n; i++) {
        if ((s[i] & 0xC0u) != 0x80u) { *cp_out = CP_INVALID; return 1; }
        cp = (cp << 6) | (s[i] & 0x3Fu);
    }
    /* 拒绝过长编码与代理区（防御；协议解析器已先行保证合法 UTF-8） */
    if ((n == 2 && cp < 0x80u) || (n == 3 && cp < 0x800u) ||
        (n == 4 && cp < 0x10000u) || (cp >= 0xD800u && cp <= 0xDFFFu)) {
        *cp_out = CP_INVALID;
        return 1;
    }
    *cp_out = cp;
    return n;
}

/* 显示宽估算：CJK/全角/常见 emoji 区段按 2 列，其余 1 列。
 * 这是布局预算估算，不追求 Unicode 宽度表完备；像素级兜底在 UI 层（dot 截断）。*/
static int cp_width(uint32_t cp)
{
    if (cp == CP_INVALID) return 1;
    if (cp < 0x1100u) return 1; /* ASCII/拉丁/控制 */
    if (cp >= 0x1100u && cp <= 0x115Fu) return 2;   /* Hangul Jamo */
    if (cp >= 0x2E80u && cp <= 0x303Eu) return 2;   /* CJK 部首/符号 */
    if (cp >= 0x3041u && cp <= 0x33FFu) return 2;   /* 假名等 */
    if (cp >= 0x3400u && cp <= 0x4DBFu) return 2;
    if (cp >= 0x4E00u && cp <= 0x9FFFu) return 2;   /* CJK 统一表意 */
    if (cp >= 0xA000u && cp <= 0xA4CFu) return 2;
    if (cp >= 0xAC00u && cp <= 0xD7A3u) return 2;   /* Hangul 音节 */
    if (cp >= 0xF900u && cp <= 0xFAFFu) return 2;
    if (cp >= 0xFE30u && cp <= 0xFE4Fu) return 2;
    if (cp >= 0xFF00u && cp <= 0xFF60u) return 2;   /* 全角形式 */
    if (cp >= 0xFFE0u && cp <= 0xFFE6u) return 2;
    if (cp >= 0x1F300u && cp <= 0x1F9FFu) return 2; /* emoji */
    if (cp >= 0x20000u && cp <= 0x3FFFDu) return 2; /* CJK 扩展 */
    return 1;
}

/*
 * 把 src 按 max_cols 显示列预算截断进 dst（dstsz 含 NUL）。
 * - 按 UTF-8 完整码点截断，绝不切断多字节序列；
 * - 发生截断时回收 2 列追加 ASCII ".."（本阶段字体为 ASCII Montserrat；
 *   "…" 等字形属 P2.2 字体任务）；
 * - 控制字符（<0x20）折为空格，避免破坏单行布局；
 * - 非法字节按 1 列原样透传（解析器已保证合法，防御而已）。
 */
static void trunc_cols(char *dst, size_t dstsz, const char *src, int max_cols)
{
    const uint8_t *p = (const uint8_t *)src;
    size_t avail = strlen(src);
    size_t out = 0;
    int cols = 0;
    bool truncated = false;

    if (dstsz == 0) return;

    while (*p != '\0' && avail > 0) {
        uint32_t cp;
        size_t n = utf8_decode(p, avail, &cp);
        int w = cp_width(cp);
        uint8_t emit[4];
        size_t emit_n = n, i;

        if (cp == CP_INVALID) {
            emit[0] = p[0];
            emit_n = 1;
        }
        else if (cp < 0x20u) {
            emit[0] = (uint8_t)' '; /* 控制字符 → 空格 */
            emit_n = 1;
        }
        else {
            for (i = 0; i < n; i++) emit[i] = p[i];
        }

        if (cols + w > max_cols) {
            truncated = true;
            break;
        }
        if (out + emit_n + 1 > dstsz) { /* 缓冲兜底（预算应保证不会到这） */
            truncated = true;
            break;
        }
        for (i = 0; i < emit_n; i++) dst[out++] = (char)emit[i];
        cols += w;
        p += n;
        avail -= n;
    }
    dst[out] = '\0';

    if (truncated) {
        /* 回收 2 列给 ".."：从尾部按码点回退 */
        while (cols > max_cols - 2 && out > 0) {
            size_t back = 1;
            while (out > 0 && ((uint8_t)dst[out - 1] & 0xC0u) == 0x80u) {
                out--;
                back++;
            }
            if (out == 0) break;
            out--; /* 回退首字节 */
            /* 重新估该码点列宽（取首字节判断） */
            {
                uint32_t cp;
                utf8_decode((const uint8_t *)&dst[out], back, &cp);
                cols -= cp_width(cp);
                if (cols < 0) cols = 0;
            }
        }
        dst[out] = '\0';
        if (out + 3 <= dstsz) {
            dst[out++] = '.';
            dst[out++] = '.';
            dst[out] = '\0';
        }
    }
}

/* ================================================================== */
/* 其他小工具                                                          */
/* ================================================================== */

static void set_str(char *dst, size_t dstsz, const char *src)
{
    snprintf(dst, dstsz, "%s", src ? src : "");
}

/* 毫秒 → "mm:ss" / "hh:mm:ss"（小时无上限，%02u 自然进位）*/
static void fmt_duration(char *dst, size_t dstsz, uint64_t ms)
{
    uint64_t total_s = ms / 1000u;
    uint64_t h = total_s / 3600u;
    uint32_t m = (uint32_t)((total_s % 3600u) / 60u);
    uint32_t s = (uint32_t)(total_s % 60u);

    if (h > 0) snprintf(dst, dstsz, "%02u:%02u:%02u", (unsigned)h, (unsigned)m, (unsigned)s);
    else snprintf(dst, dstsz, "%02u:%02u", (unsigned)m, (unsigned)s);
}

static const char *status_label_of(cdt_thread_state_t st)
{
    switch (st) {
        case CDT_THREAD_STATE_IDLE: return "IDLE";
        case CDT_THREAD_STATE_THINKING: return "THINKING";
        case CDT_THREAD_STATE_WORKING: return "WORKING";
        case CDT_THREAD_STATE_NEEDS_YOU: return "NEEDS YOU";
        case CDT_THREAD_STATE_DONE: return "DONE";
        case CDT_THREAD_STATE_ERROR: return "ERROR";
        default: return "--";
    }
}

/* §6 AGENTS 行：needs_you > error > working/thinking > done > idle */
static int agents_rank(cdt_thread_state_t st)
{
    switch (st) {
        case CDT_THREAD_STATE_NEEDS_YOU: return 0;
        case CDT_THREAD_STATE_ERROR: return 1;
        case CDT_THREAD_STATE_WORKING: return 2;
        case CDT_THREAD_STATE_THINKING: return 2;
        case CDT_THREAD_STATE_DONE: return 3;
        case CDT_THREAD_STATE_IDLE: return 4;
        default: return 5;
    }
}

/* §6 AGENTS 行排序比较：<0 = a 在前。同优先级按 updated_at 降序、id 升序。 */
static int agents_cmp(const cdt_thread_t *a, const cdt_thread_t *b)
{
    int ra = agents_rank(a->state);
    int rb = agents_rank(b->state);
    int64_t ta = a->updated_at_ms_present ? a->updated_at_ms : 0;
    int64_t tb = b->updated_at_ms_present ? b->updated_at_ms : 0;

    if (ra != rb) return ra < rb ? -1 : 1;
    if (ta != tb) return ta > tb ? -1 : 1;
    return strcmp(a->id, b->id);
}

/* ================================================================== */
/* cdt_present                                                         */
/* ================================================================== */

void cdt_present(const cdt_app_state_t *state,
                 const cdt_runtime_t *rt,
                 uint32_t now_monotonic_ms,
                 cdt_view_t *view)
{
    cdt_runtime_t rt_default;
    const cdt_thread_t *th = NULL;
    bool source_fresh, link_fresh, fresh;
    uint32_t delta_ms = 0;

    if (view == NULL) return;
    memset(view, 0, sizeof(*view));
    view->agents_pages = 1; /* 空数据也按单页处理（UI 显示空态） */
    view->plan_pages = 1;

    if (rt == NULL) { /* 防御：全默认 runtime */
        memset(&rt_default, 0, sizeof(rt_default));
        rt_default.power_state = CDT_POWER_INVALID;
        rt_default.link_state = CDT_LINK_INVALID;
        rt_default.selected_page = CDT_PAGE_NOW;
        rt = &rt_default;
    }

    /* ---- 电压（§7.1 UI 优先显示电压；invalid → "--"）---- */
    view->battery_valid = rt->battery_valid;
    view->usable_percent = rt->usable_percent;
    if (rt->battery_valid) {
        snprintf(view->voltage_text, sizeof(view->voltage_text), "%u.%02uV",
                 (unsigned)(rt->battery_mv / 1000u), (unsigned)((rt->battery_mv % 1000u) / 10u));
    }
    else {
        set_str(view->voltage_text, sizeof(view->voltage_text), "--");
    }

    view->muted = rt->muted_attention_present;

    /* ---- 页面裁决：电池 critical / sleep_prep 强制 LOW_BATTERY（P2.1 只出标记）---- */
    if (rt->power_state == CDT_POWER_CRITICAL || rt->power_state == CDT_POWER_SLEEP_PREP) {
        view->page = CDT_PAGE_LOW_BATTERY;
        view->low_battery_forced = true;
        view->status = CDT_THREAD_STATE_INVALID;
        set_str(view->status_label, sizeof(view->status_label), "LOW BATTERY");
        view->status_emphasized = true;
    }
    else {
        view->page = (rt->selected_page == CDT_PAGE_INVALID) ? CDT_PAGE_NOW : rt->selected_page;
    }

    /* ---- 链路提示位（独立于业务状态）---- */
    view->link_disconnected = (rt->link_state == CDT_LINK_DISCONNECTED);
    view->link_stale = !view->link_disconnected &&
                       ((rt->link_state == CDT_LINK_STALE) ||
                        (state != NULL && state->source.stale));

    /* ---- fresh 语义：source 与 link 均新鲜才推进计时（§4）---- */
    source_fresh = (state != NULL) && state->source.connected && !state->source.stale;
    link_fresh = (rt->link_state == CDT_LINK_CONNECTED);
    fresh = source_fresh && link_fresh;
    view->time_frozen = !fresh;
    if (fresh && now_monotonic_ms >= rt->last_rx_monotonic_ms) {
        delta_ms = now_monotonic_ms - rt->last_rx_monotonic_ms;
    }

    /* ---- 选中线程：selected 优先，否则首项（§3 排序默认选首项）---- */
    if (state != NULL && state->thread_count > 0) {
        uint8_t i;
        if (state->selected_thread_id_present) {
            for (i = 0; i < state->thread_count; i++) {
                if (strcmp(state->threads[i].id, state->selected_thread_id) == 0) {
                    th = &state->threads[i];
                    break;
                }
            }
        }
        if (th == NULL) th = &state->threads[0];
    }

    view->threads_total = state ? state->threads_total : 0;

    /* ---- 尚无快照：全部 "--" ---- */
    if (state == NULL) {
        set_str(view->project, sizeof(view->project), "--");
        set_str(view->activity, sizeof(view->activity), "--");
        set_str(view->status_label, sizeof(view->status_label), "--");
        set_str(view->elapsed_text, sizeof(view->elapsed_text), "--");
        set_str(view->waiting_text, sizeof(view->waiting_text), "--");
        set_str(view->usage_text, sizeof(view->usage_text), "--");
        return;
    }

    /* ---- 无任务：IDLE（§6 无任务 IDLE）---- */
    if (th == NULL) {
        if (!view->low_battery_forced) {
            view->status = CDT_THREAD_STATE_IDLE;
            set_str(view->status_label, sizeof(view->status_label), "IDLE");
        }
        set_str(view->project, sizeof(view->project), "--");
        set_str(view->activity, sizeof(view->activity), "--");
        set_str(view->elapsed_text, sizeof(view->elapsed_text), "--");
        set_str(view->waiting_text, sizeof(view->waiting_text), "--");
        /* 额度独立于线程，仍按 usage 显示 */
        goto usage_line;
    }

    /* ---- 业务状态词（低压强制页不覆盖状态词区域）---- */
    view->cancelled = (th->end_reason == CDT_END_REASON_CANCELLED);
    if (!view->low_battery_forced) {
        if (view->cancelled) {
            /* §6：取消显示 IDLE + 已取消 */
            view->status = CDT_THREAD_STATE_IDLE;
            set_str(view->status_label, sizeof(view->status_label), "IDLE");
        }
        else {
            view->status = th->state;
            set_str(view->status_label, sizeof(view->status_label), status_label_of(th->state));
        }
        view->status_emphasized = (th->state == CDT_THREAD_STATE_NEEDS_YOU ||
                                   th->state == CDT_THREAD_STATE_ERROR);
    }

    /* ---- P2.2：AGENTS 行（全部可见线程按 §6 排序：needs_you > error >
     * working/thinking > done > idle；同级 updated_at 降序、id 升序。
     * 插入排序索引数组（n≤8），再按序填充行副本。---- */
    {
        uint8_t order[CDT_MAX_THREADS];
        uint8_t i, j;
        uint8_t n = state->thread_count;

        view->agents_count = n;
        view->agents_hidden = (uint16_t)(state->threads_total > (uint16_t)n
                                             ? state->threads_total - (uint16_t)n
                                             : 0);
        view->threads_truncated = state->threads_truncated;
        for (i = 0; i < n; i++) order[i] = i;
        for (i = 1; i < n; i++) {
            uint8_t key = order[i];
            for (j = i; j > 0 && agents_cmp(&state->threads[order[j - 1]],
                                            &state->threads[key]) > 0; j--) {
                order[j] = order[j - 1];
            }
            order[j] = key;
        }
        for (i = 0; i < n; i++) {
            const cdt_thread_t *src = &state->threads[order[i]];
            cdt_agents_row_t *row = &view->agents_rows[i];

            row->state = src->state;
            set_str(row->state_label, sizeof(row->state_label), status_label_of(src->state));
            if (src->project[0] != '\0') {
                trunc_cols(row->project, sizeof(row->project), src->project,
                           CDT_VIEW_AGENTS_PROJECT_MAX_COLS);
            }
            else {
                set_str(row->project, sizeof(row->project), "--");
            }
            row->waiting = (src->state == CDT_THREAD_STATE_NEEDS_YOU);
            row->emphasized = (src->state == CDT_THREAD_STATE_NEEDS_YOU ||
                               src->state == CDT_THREAD_STATE_ERROR);
        }
        view->agents_pages = (uint8_t)((n + CDT_VIEW_ROWS_PER_PAGE - 1) /
                                       CDT_VIEW_ROWS_PER_PAGE);
        if (view->agents_pages == 0) view->agents_pages = 1;
    }

    /* ---- 文本（presenter 按列预算截断）---- */
    trunc_cols(view->project, sizeof(view->project), th->project, CDT_VIEW_PROJECT_MAX_COLS);
    if (th->project[0] == '\0') set_str(view->project, sizeof(view->project), "--");

    if (th->activity[0] != '\0') {
        trunc_cols(view->activity, sizeof(view->activity), th->activity, CDT_VIEW_ACTIVITY_MAX_COLS);
    }
    else {
        set_str(view->activity, sizeof(view->activity), "--");
    }

    view->pending_count = th->attention_present ? th->attention.pending_count : 0;
    if (th->attention_present && th->attention.pending_count > 0) {
        view->attention_present = true;
        trunc_cols(view->attention, sizeof(view->attention), th->attention.summary,
                   CDT_VIEW_ATTENTION_MAX_COLS);
    }

    /* ---- 时长：base + fresh 增量（陈旧冻结）----
     * 终态（done/error/cancelled，end_reason 非空）任务时长定格在快照基值：
     * 终态后时长不再推进（SCENARIOS S06；任务已结束，无新可计时长）。*/
    if (th->end_reason != CDT_END_REASON_NULL) {
        delta_ms = 0;
    }
    fmt_duration(view->elapsed_text, sizeof(view->elapsed_text),
                 (uint64_t)th->elapsed_ms + delta_ms);
    fmt_duration(view->waiting_text, sizeof(view->waiting_text),
                 (uint64_t)th->waiting_ms + delta_ms);

    /* ---- P2.2：PLAN 页步骤（原始顺序；只数 completed；分页计数）----
     * NOW 摘要 "PLAN c/t" 与 PLAN 页共用同一个 completed 计数。 */
    {
        uint16_t completed = 0;
        uint8_t k;

        for (k = 0; k < th->plan.step_count; k++) {
            if ((cdt_step_status_t)th->plan.steps[k].status == CDT_STEP_STATUS_COMPLETED) {
                completed++;
            }
        }
        view->plan_completed = (uint8_t)(completed > 255u ? 255u : completed);
        view->plan_total = th->plan.total;
        view->plan_truncated = th->plan.truncated;
        view->plan_step_count = th->plan.step_count;
        for (k = 0; k < th->plan.step_count; k++) {
            cdt_plan_step_row_t *row = &view->plan_steps[k];
            if (th->plan.steps[k].text[0] != '\0') {
                trunc_cols(row->text, sizeof(row->text), th->plan.steps[k].text,
                           CDT_VIEW_STEP_MAX_COLS);
            }
            else {
                set_str(row->text, sizeof(row->text), "--");
            }
            row->status = th->plan.steps[k].status;
        }
        view->plan_pages = (uint8_t)((th->plan.step_count + CDT_VIEW_ROWS_PER_PAGE - 1) /
                                     CDT_VIEW_ROWS_PER_PAGE);
        if (view->plan_pages == 0) view->plan_pages = 1; /* 空计划单页（暂无计划） */
    }

    /* ---- 计划摘要 "PLAN c/t"（completed/total；total==0 → 无计划隐藏）---- */
    if (th->plan.total > 0) {
        snprintf(view->plan_text, sizeof(view->plan_text), "PLAN %u/%u",
                 (unsigned)view->plan_completed, (unsigned)th->plan.total);
        view->plan_present = true;
    }

usage_line:
    /* ---- 业务时钟（USAGE reset 倒计时基准）：快照 generated_at_ms +
     * fresh 单调增量；陈旧/缺失时冻结在 generated_at_ms（不猜新值）。
     * generated_at 缺失 → 倒计时不可知（reset_present=false，UI 显示 RST --）。*/
    {
        int64_t business_now_ms = 0;
        bool business_time_known = state->generated_at_ms_present;

        if (business_time_known) {
            business_now_ms = state->generated_at_ms + (int64_t)delta_ms;
        }

        /* ---- P2.2：USAGE 逐窗口行（label 取自数据，不编造窗口名）---- */
        view->usage_count = 0;
        if (state->usage.available) {
            uint8_t k;
            for (k = 0; k < state->usage.window_count; k++) {
                const cdt_usage_window_t *src = &state->usage.windows[k];
                cdt_usage_row_t *row = &view->usage_rows[view->usage_count];

                if (src->label[0] != '\0') {
                    trunc_cols(row->label, sizeof(row->label), src->label,
                               CDT_VIEW_WIN_LABEL_MAX_COLS);
                }
                else {
                    set_str(row->label, sizeof(row->label), "--");
                }
                if (src->used_percent_present) {
                    unsigned pct = (unsigned)(src->used_percent + 0.5); /* 四舍五入显示 */
                    if (pct > 100u) pct = 100u;
                    row->pct_present = true;
                    row->pct = (uint8_t)pct;
                }
                else {
                    row->pct_present = false;
                    row->pct = 0;
                }
                row->duration_mins = src->duration_mins;
                if (src->resets_at_ms_present && business_time_known) {
                    int64_t diff_s = (src->resets_at_ms - business_now_ms) / 1000;
                    /* 防溢出钳制；<0 = 已过 reset（UI 显示 EXPIRED，不猜 0%） */
                    if (diff_s > (int64_t)INT32_MAX) diff_s = INT32_MAX;
                    if (diff_s < (int64_t)INT32_MIN) diff_s = INT32_MIN;
                    row->reset_present = true;
                    row->reset_in_s = (int32_t)diff_s;
                }
                else {
                    row->reset_present = false;
                    row->reset_in_s = 0;
                }
                view->usage_count++;
            }
        }

        /* ---- P2.2：context 独立行（选中任务；只有可信百分比才算占用；
         * 累计 token 不能冒充 context → 无百分比一律 "CTX --"）---- */
        if (th != NULL && th->context.used_percent_present) {
            unsigned pct = (unsigned)(th->context.used_percent + 0.5);
            if (pct > 100u) pct = 100u;
            snprintf(view->context_text, sizeof(view->context_text), "CTX %u%%", pct);
        }
        else {
            set_str(view->context_text, sizeof(view->context_text), "CTX --");
        }
    }

    /* ---- 额度摘要：首窗口 label + used%，多窗口加 "+N"；缺失 → "--" ---- */
    if (state->usage.available && state->usage.window_count > 0) {
        const cdt_usage_window_t *w = &state->usage.windows[0];
        char line[CDT_VIEW_USAGE_BYTES];

        trunc_cols(line, sizeof(line), w->label, CDT_VIEW_USAGE_MAX_COLS - 13);
        if (w->used_percent_present) {
            unsigned pct = (unsigned)(w->used_percent + 0.5); /* 四舍五入到整数显示 */
            if (pct > 100u) pct = 100u;
            snprintf(view->usage_text, sizeof(view->usage_text), "%s %u%%", line, pct);
        }
        else {
            snprintf(view->usage_text, sizeof(view->usage_text), "%s --", line);
        }
        if (state->usage.windows_total > 1) {
            char tail[16];
            snprintf(tail, sizeof(tail), " +%u", (unsigned)(state->usage.windows_total - 1u));
            size_t len = strlen(view->usage_text);
            if (len + strlen(tail) < sizeof(view->usage_text)) {
                strcat(view->usage_text, tail);
            }
        }
    }
    else {
        set_str(view->usage_text, sizeof(view->usage_text), "--");
    }
}
