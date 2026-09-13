/*
 * test_presenter.c — Presenter 纯转换主机端测试（P2.1，A2）
 *
 * 用法：test_presenter（无参数；全部用例内置，直接构造 C 结构，不读 JSON）
 * 覆盖（P2.1 验收 + INTERFACES §4）：
 *   - 优先级：battery critical/sleep_prep → LOW_BATTERY 强制页标记
 *   - "--"：电压无效/无额度/无快照/无任务
 *   - fresh/frozen 计时：source+link 均 fresh 才推进 elapsed/waiting，
 *     link stale / disconnected / source.stale 冻结
 *   - 时长格式 mm:ss / hh:mm:ss
 *   - 六状态词与 needs_you/error 强调位；cancelled → IDLE+已取消
 *   - 计划/额度摘要；长文按列预算截断（ASCII 与 CJK UTF-8 码点安全）
 * 任何 FAIL 退出码 1。
 */
#include <stdio.h>
#include <string.h>

#include "cdt_presenter.h"

static int failures = 0;
static int passes = 0;

static void check(int cond, const char *name, const char *detail)
{
    if (cond) {
        passes++;
        printf("[PASS] %s\n", name);
    }
    else {
        failures++;
        printf("[FAIL] %s — %s\n", name, detail ? detail : "");
    }
}

/* ---------- 基准 AppState / Runtime ---------- */

static void base_thread(cdt_thread_t *t)
{
    memset(t, 0, sizeof(*t));
    snprintf(t->id, sizeof(t->id), "thread-1");
    t->turn_id_present = true;
    snprintf(t->turn_id, sizeof(t->turn_id), "turn-1");
    snprintf(t->project, sizeof(t->project), "codex-desk-terminal");
    t->state = CDT_THREAD_STATE_WORKING;
    snprintf(t->activity, sizeof(t->activity), "editing main.c");
    t->elapsed_ms = 120000;
    t->waiting_ms = 5000;
    t->end_reason = CDT_END_REASON_NULL;
    t->plan.total = 3;
    t->plan.truncated = false;
    t->plan.step_count = 3;
    snprintf(t->plan.steps[0].text, sizeof(t->plan.steps[0].text), "s0");
    t->plan.steps[0].status = CDT_STEP_STATUS_COMPLETED;
    snprintf(t->plan.steps[1].text, sizeof(t->plan.steps[1].text), "s1");
    t->plan.steps[1].status = CDT_STEP_STATUS_IN_PROGRESS;
    snprintf(t->plan.steps[2].text, sizeof(t->plan.steps[2].text), "s2");
    t->plan.steps[2].status = CDT_STEP_STATUS_PENDING;
}

static cdt_app_state_t base_state(void)
{
    cdt_app_state_t s;
    memset(&s, 0, sizeof(s));
    snprintf(s.bridge_epoch, sizeof(s.bridge_epoch), "test-epoch");
    s.seq = 1;
    s.source.kind = CDT_SOURCE_MOCK;
    s.source.connected = true;
    s.source.stale = false;
    s.selected_thread_id_present = true;
    snprintf(s.selected_thread_id, sizeof(s.selected_thread_id), "thread-1");
    s.threads_total = 1;
    s.thread_count = 1;
    base_thread(&s.threads[0]);
    s.usage.available = true;
    s.usage.windows_total = 1;
    s.usage.window_count = 1;
    snprintf(s.usage.windows[0].label, sizeof(s.usage.windows[0].label), "SHORT WINDOW");
    s.usage.windows[0].used_percent_present = true;
    s.usage.windows[0].used_percent = 42.5;
    s.usage.windows[0].duration_mins = 300;
    return s;
}

static cdt_runtime_t base_rt(void)
{
    cdt_runtime_t r;
    memset(&r, 0, sizeof(r));
    r.battery_valid = true;
    r.battery_mv = 3900;
    r.usable_percent = 50;
    r.charging = CDT_PRESENCE_UNKNOWN;
    r.external_power = CDT_PRESENCE_UNKNOWN;
    r.power_state = CDT_POWER_ACTIVE;
    snprintf(r.transport, sizeof(r.transport), "mock");
    r.link_state = CDT_LINK_CONNECTED;
    r.last_rx_monotonic_ms = 30000;
    r.selected_page = CDT_PAGE_NOW;
    return r;
}

/* UTF-8 合法性检查（截断不得产生断裂序列）*/
static int valid_utf8(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        unsigned char b = *p;
        size_t n, i;
        if (b < 0x80u) { p++; continue; }
        if ((b & 0xE0u) == 0xC0u) n = 2;
        else if ((b & 0xF0u) == 0xE0u) n = 3;
        else if ((b & 0xF8u) == 0xF0u) n = 4;
        else return 0;
        for (i = 1; i < n; i++) {
            if ((p[i] & 0xC0u) != 0x80u) return 0;
        }
        p += n;
    }
    return 1;
}

/* 显示列估算（与 presenter 同规则的独立简化实现，测截断预算）*/
static int display_cols(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    int cols = 0;
    while (*p) {
        int n = 1;
        if (*p >= 0xF0u) n = 4;
        else if (*p >= 0xE0u) n = 3;
        else if (*p >= 0xC0u) n = 2;
        cols += (n >= 3) ? 2 : 1; /* 测试输入只有 ASCII 与 3 字节 CJK */
        p += n;
    }
    return cols;
}

/* ---------- 用例 ---------- */

static void test_priority_low_battery(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    r.power_state = CDT_POWER_CRITICAL;
    cdt_present(&s, &r, 40000, &v);
    check(v.page == CDT_PAGE_LOW_BATTERY && v.low_battery_forced,
          "critical → LOW_BATTERY 强制页标记", "page/flag 不符");
    check(strcmp(v.status_label, "LOW BATTERY") == 0 && v.status_emphasized,
          "critical → 状态词 LOW BATTERY 且强调", v.status_label);
    check(strcmp(v.voltage_text, "3.90V") == 0 && v.battery_valid,
          "低压强制页仍保留基本字段（电压）", v.voltage_text);
}

static void test_priority_sleep_prep(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    r.power_state = CDT_POWER_SLEEP_PREP;
    cdt_present(&s, &r, 40000, &v);
    check(v.low_battery_forced && v.page == CDT_PAGE_LOW_BATTERY,
          "sleep_prep → LOW_BATTERY 强制页标记", "page/flag 不符");

    r.power_state = CDT_POWER_ACTIVE;
    cdt_present(&s, &r, 40000, &v);
    check(!v.low_battery_forced && v.page == CDT_PAGE_NOW,
          "ACTIVE → 普通页 NOW，不强制", "page/flag 不符");
}

static void test_voltage(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    r.battery_mv = 3590; /* < 3600：0% */
    r.usable_percent = 0;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.voltage_text, "3.59V") == 0 && v.usable_percent == 0,
          "电压 3590mV → \"3.59V\"，可用 0%", v.voltage_text);

    r.battery_mv = 4200;
    r.usable_percent = 100;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.voltage_text, "4.20V") == 0 && v.usable_percent == 100,
          "电压 4200mV → \"4.20V\"，可用 100%", v.voltage_text);

    r.battery_valid = false;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.voltage_text, "--") == 0 && !v.battery_valid,
          "电池无效 → 电压 \"--\"", v.voltage_text);
}

static void test_status_words(void)
{
    /* ZC6：needs_you → 专用警报布局（alarm_mode），其余有效任务态 →
     * 状态词左对齐实心圆点（status_dot） */
    struct {
        cdt_thread_state_t st;
        const char *label;
        int emph;
        int dot;
        int alarm;
    } cases[] = {
        { CDT_THREAD_STATE_IDLE, "IDLE", 0, 1, 0 },
        { CDT_THREAD_STATE_THINKING, "THINKING", 0, 1, 0 },
        { CDT_THREAD_STATE_WORKING, "WORKING", 0, 1, 0 },
        { CDT_THREAD_STATE_NEEDS_YOU, "NEEDS YOU", 1, 0, 1 },
        { CDT_THREAD_STATE_DONE, "DONE", 0, 1, 0 },
        { CDT_THREAD_STATE_ERROR, "ERROR", 1, 1, 0 },
    };
    size_t i;
    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        cdt_app_state_t s = base_state();
        cdt_runtime_t r = base_rt();
        cdt_view_t v;
        char name[64];
        s.threads[0].state = cases[i].st;
        cdt_present(&s, &r, 40000, &v);
        snprintf(name, sizeof(name), "状态词 %s（emphasized=%d dot=%d alarm=%d）",
                 cases[i].label, cases[i].emph, cases[i].dot, cases[i].alarm);
        check(strcmp(v.status_label, cases[i].label) == 0 &&
                  v.status_emphasized == cases[i].emph &&
                  v.status == cases[i].st &&
                  v.status_dot == cases[i].dot &&
                  v.alarm_mode == cases[i].alarm &&
                  v.elapsed_present,
              name, v.status_label);
    }
}

static void test_cancelled(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    s.threads[0].state = CDT_THREAD_STATE_IDLE;
    s.threads[0].end_reason = CDT_END_REASON_CANCELLED;
    cdt_present(&s, &r, 40000, &v);
    check(v.status == CDT_THREAD_STATE_IDLE && strcmp(v.status_label, "IDLE") == 0 && v.cancelled,
          "cancelled → IDLE + 已取消标记", v.status_label);
    /* ZC6：cancelled 不进警报布局（→ 常规态圆点 + RUNNING FOR 行） */
    check(!v.alarm_mode && v.status_dot && v.elapsed_present,
          "cancelled → 非警报布局（常规态：dot + running-for）", "");
}

static void test_no_tasks_and_no_state(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    s.thread_count = 0;
    s.threads_total = 0;
    s.selected_thread_id_present = false;
    cdt_present(&s, &r, 40000, &v);
    check(v.status == CDT_THREAD_STATE_IDLE && strcmp(v.status_label, "IDLE") == 0,
          "无任务 → IDLE", v.status_label);
    check(strcmp(v.project, "--") == 0 && strcmp(v.activity, "--") == 0 &&
              strcmp(v.elapsed_text, "--") == 0,
          "无任务 → 项目/活动/时长 \"--\"", v.project);
    /* ZC6：无任务 → 无圆点、无警报布局、无 RUNNING FOR 行 */
    check(!v.status_dot && !v.alarm_mode && !v.elapsed_present,
          "无任务 → dot/alarm/running 全否", "");

    cdt_present(NULL, &r, 40000, &v);
    check(strcmp(v.status_label, "--") == 0 && strcmp(v.project, "--") == 0 &&
              strcmp(v.usage_text, "--") == 0 && strcmp(v.elapsed_text, "--") == 0,
          "无快照 → 状态/项目/额度/时长 \"--\"", v.status_label);
    check(!v.status_dot && !v.alarm_mode && !v.elapsed_present,
          "无快照 → dot/alarm/running 全否", "");
    check(strcmp(v.voltage_text, "3.90V") == 0,
          "无快照时电压仍来自 runtime（有效→显示）", v.voltage_text);

    r.battery_valid = false;
    cdt_present(NULL, &r, 40000, &v);
    check(strcmp(v.voltage_text, "--") == 0,
          "无快照且电池无效 → 电压 \"--\"", v.voltage_text);
}

static void test_usage_and_plan(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.usage_text, "SHORT WINDOW 43%") == 0,
          "额度摘要 label + 取整百分比", v.usage_text);
    check(strcmp(v.plan_text, "PLAN 1/3") == 0 && v.plan_present,
          "计划摘要 PLAN 1/3", v.plan_text);

    s.usage.windows_total = 3;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.usage_text, "SHORT WINDOW 43% +2") == 0,
          "多窗口 → 追加 +2", v.usage_text);

    s.usage.available = false;
    s.usage.window_count = 0;
    s.usage.windows_total = 0;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.usage_text, "--") == 0, "无额度 → \"--\"", v.usage_text);

    s.usage.available = true;
    s.usage.window_count = 1;
    s.usage.windows_total = 1;
    s.usage.windows[0].used_percent_present = false;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.usage_text, "SHORT WINDOW --") == 0,
          "percent null → \"--\"", v.usage_text);

    s.threads[0].plan.total = 0;
    s.threads[0].plan.step_count = 0;
    cdt_present(&s, &r, 40000, &v);
    check(!v.plan_present, "无计划 → plan_present=false（UI 隐藏行）", "");
    check(!v.plan_current_present, "ZC7 无计划 → 当前步详情隐藏", "");
}

static void test_usage_blocks_and_plan_current(void)
{
    /* ZC7：USAGE 图形化窗口块（标签行/倒计时行）+ PLAN 当前步详情（效果图 4/5）。 */
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* 基准：300min 整小时窗口、42.5%；generated_at 缺失 → 倒计时不可知 */
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.usage_rows[0].title, "5 HOUR WINDOW") == 0,
          "ZC7 300min → \"5 HOUR WINDOW\"", v.usage_rows[0].title);
    check(strcmp(v.usage_rows[0].reset_text, "RESET --") == 0,
          "ZC7 generated_at 缺失 → \"RESET --\"（不猜倒计时）",
          v.usage_rows[0].reset_text);
    check(v.plan_current_present && v.plan_current_index == 1 &&
              strcmp(v.plan_current_text, "s1") == 0,
          "ZC7 当前步详情=in_progress 步骤 s1（index 1）", v.plan_current_text);

    /* 业务时钟已知：<1h → hh:mm:ss；≥1h → h:mm（秒不冒充） */
    s.generated_at_ms_present = true;
    s.generated_at_ms = 1789002000000;
    s.usage.windows[0].resets_at_ms_present = true;
    s.usage.windows[0].resets_at_ms = s.generated_at_ms + 3599000; /* 59m59s */
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.usage_rows[0].reset_text, "RESET IN 00:59:59") == 0,
          "ZC7 3599s → \"RESET IN 00:59:59\"", v.usage_rows[0].reset_text);

    s.usage.windows[0].resets_at_ms = s.generated_at_ms + (int64_t)7554 * 1000;
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.usage_rows[0].reset_text, "RESET IN 2:05") == 0,
          "ZC7 7554s → \"RESET IN 2:05\"（≥1h h:mm）", v.usage_rows[0].reset_text);

    /* 已过 reset → EXPIRED */
    s.usage.windows[0].resets_at_ms = s.generated_at_ms - 60000;
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.usage_rows[0].reset_text, "RESET EXPIRED") == 0,
          "ZC7 已过 reset → \"RESET EXPIRED\"", v.usage_rows[0].reset_text);

    /* 非整小时窗口 → "<M> MIN WINDOW"；45.9 → 46（条形与百分比同源取整） */
    s.usage.windows[0].duration_mins = 45;
    s.usage.windows[0].used_percent = 45.9;
    s.usage.windows[0].resets_at_ms = s.generated_at_ms + 3600000;
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.usage_rows[0].title, "45 MIN WINDOW") == 0,
          "ZC7 45min → \"45 MIN WINDOW\"", v.usage_rows[0].title);
    check(v.usage_rows[0].pct == 46, "ZC7 45.9 → 46（图形条填充同源）", "");

    /* pct null → pct_present=false（UI 条画空 + 右显 "--"），标签行仍在 */
    s.usage.windows[0].used_percent_present = false;
    cdt_present(&s, &r, 30000, &v);
    check(!v.usage_rows[0].pct_present && v.usage_rows[0].pct == 0,
          "ZC7 pct null → 条画空（0 段）+ 右显 \"--\"", "");

    /* 全 completed → 无当前步；无 in_progress 有 pending → 首个 pending */
    s.threads[0].plan.steps[1].status = CDT_STEP_STATUS_COMPLETED;
    s.threads[0].plan.steps[2].status = CDT_STEP_STATUS_COMPLETED;
    cdt_present(&s, &r, 30000, &v);
    check(!v.plan_current_present, "ZC7 全 completed → 当前步详情隐藏", "");

    s.threads[0].plan.steps[0].status = CDT_STEP_STATUS_PENDING;
    s.threads[0].plan.steps[1].status = CDT_STEP_STATUS_PENDING;
    cdt_present(&s, &r, 30000, &v);
    check(v.plan_current_present && v.plan_current_index == 0 &&
              strcmp(v.plan_current_text, "s0") == 0,
          "ZC7 无 in_progress → 首个 pending（index 0）", v.plan_current_text);

    /* 详情面板列预算（66 列）比列表行（34 列）宽：长文本截断上限更大 */
    snprintf(s.threads[0].plan.steps[0].text, sizeof(s.threads[0].plan.steps[0].text),
             "%s", "step text long enough to exceed thirty-four display columns");
    cdt_present(&s, &r, 30000, &v);
    check(v.plan_current_present &&
              strlen(v.plan_current_text) > strlen(v.plan_steps[0].text) &&
              valid_utf8(v.plan_current_text),
          "ZC7 详情 66 列 > 列表行 34 列（码点安全）", v.plan_current_text);
}

static void test_fresh_timing(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* last_rx=30000, now=40000 → base 120000ms + 10000ms = 130000ms = 02:10 */
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.elapsed_text, "02:10") == 0 && !v.time_frozen,
          "fresh：elapsed = base + (now-last_rx) → 02:10", v.elapsed_text);
    check(strcmp(v.waiting_text, "00:15") == 0,
          "fresh：waiting 同步推进 → 00:15", v.waiting_text);

    /* link stale → 冻结在 base 值 */
    r.link_state = CDT_LINK_STALE;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.elapsed_text, "02:00") == 0 && v.time_frozen && v.link_stale,
          "link stale → elapsed 冻结 02:00，link_stale 提示位", v.elapsed_text);

    /* link disconnected → 冻结 + disconnected 提示位（业务状态词不变） */
    r.link_state = CDT_LINK_DISCONNECTED;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.elapsed_text, "02:00") == 0 && v.link_disconnected &&
              strcmp(v.status_label, "WORKING") == 0,
          "link disconnected → 冻结且业务状态词不被覆盖", v.elapsed_text);

    /* 上游 source.stale → 冻结（即使 link connected） */
    r.link_state = CDT_LINK_CONNECTED;
    s.source.stale = true;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.elapsed_text, "02:00") == 0 && v.time_frozen && v.link_stale,
          "source.stale → 冻结 + stale 提示位", v.elapsed_text);

    /* now < last_rx（防御）：不产生巨大增量 */
    s.source.stale = false;
    cdt_present(&s, &r, 1000, &v);
    check(strcmp(v.elapsed_text, "02:00") == 0,
          "now<last_rx 防御 → 不回退不暴涨", v.elapsed_text);
}

static void test_duration_format(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    s.threads[0].elapsed_ms = 3661000; /* 1h01m01s */
    s.threads[0].waiting_ms = 0;
    cdt_present(&s, &r, 30000, &v); /* last_rx==now：无增量 */
    check(strcmp(v.elapsed_text, "01:01:01") == 0,
          "≥1h → hh:mm:ss", v.elapsed_text);
    check(strcmp(v.waiting_text, "00:00") == 0, "0ms → 00:00", v.waiting_text);

    s.threads[0].elapsed_ms = 59000; /* 59s → 00:59 */
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.elapsed_text, "00:59") == 0, "<1min → 00:59", v.elapsed_text);
}

static void test_truncation(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    char long_ascii[CDT_MAX_ACTIVITY_BYTES + 1];
    char long_cjk[CDT_MAX_ACTIVITY_BYTES + 1];
    size_t i;

    for (i = 0; i < CDT_MAX_ACTIVITY_BYTES; i++) long_ascii[i] = (char)('a' + (i % 26));
    long_ascii[CDT_MAX_ACTIVITY_BYTES] = '\0';
    strcpy(long_cjk, "");
    for (i = 0; i < 64; i++) strcat(long_cjk, "\xE7\xA0\x81"); /* 码 ×64 = 192B */

    s.threads[0].activity[0] = '\0';
    strncat(s.threads[0].activity, long_ascii, sizeof(s.threads[0].activity) - 1);
    cdt_present(&s, &r, 30000, &v);
    check(strlen(v.activity) > 0 && strlen(v.activity) <= CDT_VIEW_ACTIVITY_BYTES - 1 &&
              display_cols(v.activity) <= CDT_VIEW_ACTIVITY_MAX_COLS &&
              strcmp(v.activity + strlen(v.activity) - 2, "..") == 0,
          "ASCII 长文 → 按预算截断且以 .. 结尾", v.activity);

    s.threads[0].activity[0] = '\0';
    strncat(s.threads[0].activity, long_cjk, sizeof(s.threads[0].activity) - 1);
    cdt_present(&s, &r, 30000, &v);
    check(valid_utf8(v.activity) &&
              display_cols(v.activity) <= CDT_VIEW_ACTIVITY_MAX_COLS &&
              strcmp(v.activity + strlen(v.activity) - 2, "..") == 0,
          "CJK 长文 → UTF-8 码点安全截断（不切断序列）", v.activity);

    /* 项目名也按预算截断（标题栏给电压留位）*/
    s.threads[0].activity[0] = '\0';
    s.threads[0].project[0] = '\0';
    strncat(s.threads[0].project, long_ascii, sizeof(s.threads[0].project) - 1);
    cdt_present(&s, &r, 30000, &v);
    check(display_cols(v.project) <= CDT_VIEW_PROJECT_MAX_COLS,
          "超长项目名 → 标题栏预算截断", v.project);

    /* 空活动 → "--" */
    s.threads[0].project[0] = '\0';
    s.threads[0].activity[0] = '\0';
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.project, "--") == 0 && strcmp(v.activity, "--") == 0,
          "空 project/activity → \"--\"", v.activity);
}

static void test_attention_and_mute(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    s.threads[0].state = CDT_THREAD_STATE_NEEDS_YOU;
    s.threads[0].attention_present = true;
    s.threads[0].attention.pending_count = 2;
    snprintf(s.threads[0].attention.summary, sizeof(s.threads[0].attention.summary),
             "run command?");
    cdt_present(&s, &r, 30000, &v);
    check(v.attention_present && v.pending_count == 2 &&
              strcmp(v.attention, "run command?") == 0,
          "attention 摘要透传", v.attention);
    /* ZC6：needs_you + attention → 专用警报布局（命令盒数据即 attention） */
    check(v.alarm_mode && !v.status_dot,
          "needs_you+attention → 警报布局且无状态圆点", "");

    s.threads[0].attention_present = false;
    cdt_present(&s, &r, 30000, &v);
    check(!v.attention_present && v.pending_count == 0,
          "attention null → 隐藏，计数 0", "");
    check(v.alarm_mode, "attention null 但 needs_you → 仍警报布局（盒隐藏由 UI 裁决）",
          "");

    r.muted_attention_present = true;
    snprintf(r.muted_attention_id, sizeof(r.muted_attention_id), "thread-1");
    cdt_present(&s, &r, 30000, &v);
    check(v.muted, "runtime 静音位透传", "");
}

static void test_details_fields(void)
{
    /* ZC4：DETAILS 页 builder（v1.2 会话级 model/tokens + context 详情）。 */
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t *th = &s.threads[0];

    /* 全量数据：CONTEXT 三值全知 → "176K / 258K (68%)"；tokens K 格式化。 */
    th->model_present = true;
    snprintf(th->model, sizeof(th->model), "GLM-5.3");
    th->context.used_tokens_present = true;
    th->context.used_tokens = 180224;   /* 176K */
    th->context.capacity_tokens_present = true;
    th->context.capacity_tokens = 264192; /* 258K */
    th->context.used_percent_present = true;
    th->context.used_percent = 68.25;
    th->tokens_present = true;
    th->input_tokens_present = true;
    th->input_tokens = 84000;           /* 82K */
    th->output_tokens_present = true;
    th->output_tokens = 2048;
    th->cached_tokens_present = true;
    th->cached_tokens = 4294967295u;    /* uint32 饱和上限 → 4095M */

    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.model_text, "GLM-5.3") == 0, "MODEL 行=会话模型名", v.model_text);
    check(strcmp(v.context_detail_text, "176K / 258K (68%)") == 0,
          "CONTEXT 全知 → \"176K / 258K (68%)\"", v.context_detail_text);
    check(strcmp(v.tokens_in_text, "82K") == 0, "INPUT 84000 → \"82K\"",
          v.tokens_in_text);
    check(strcmp(v.tokens_out_text, "2K") == 0, "OUTPUT 2048 → \"2K\"（K 分支）",
          v.tokens_out_text);
    check(strcmp(v.tokens_cached_text, "4095M") == 0, "CACHED uint32 上限 → \"4095M\"",
          v.tokens_cached_text);

    /* 只有 used：→ "578K TOKENS"。 */
    th->context.capacity_tokens_present = false;
    th->context.used_percent_present = false;
    th->context.used_tokens = 591872; /* 578K */
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.context_detail_text, "578K TOKENS") == 0,
          "仅 used → \"578K TOKENS\"（累计不冒充百分比）", v.context_detail_text);

    /* 旧桥（v1.2 前字段缺失）→ MODEL/tokens 全 "--"。 */
    th->model_present = false;
    th->tokens_present = false;
    th->context.used_tokens_present = false;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.model_text, "--") == 0 && strcmp(v.tokens_in_text, "--") == 0 &&
              strcmp(v.tokens_out_text, "--") == 0 &&
              strcmp(v.tokens_cached_text, "--") == 0 &&
              strcmp(v.context_detail_text, "--") == 0,
          "v1.2 字段缺失 → MODEL/tokens/CONTEXT 全 \"--\"", v.model_text);

    /* 无任务/无快照 → 同样 "--"（不编造）。 */
    cdt_present(NULL, &r, 40000, &v);
    check(strcmp(v.model_text, "--") == 0 && strcmp(v.context_detail_text, "--") == 0 &&
              strcmp(v.tokens_in_text, "--") == 0,
          "无快照 → DETAILS 行 \"--\"", v.model_text);
}

/* ---- A0：顶栏时钟（generated_at_ms + runtime 时区偏移，仅显示换算）---- */
static void test_header_clock(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* generated_at 缺失 → 时钟空串；电压独占右槽 */
    cdt_present(&s, &r, 40000, &v);
    check(v.clock_text[0] == '\0', "generated_at 缺失 → 时钟空串", v.clock_text);

    /* tz=0：UTC 直接渲染（模拟器确定性路径）。
     * generated_at=1789297800000 = 11:10:00 UTC。 */
    s.generated_at_ms_present = true;
    s.generated_at_ms = (int64_t)1789297800000LL;
    r.battery_valid = true;
    r.battery_mv = 3900;
    r.tz_offset_min = 0;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.clock_text, "11:10") == 0, "tz=0 → UTC 11:10", v.clock_text);
    check(strcmp(v.voltage_text, "11:10 3.90V") == 0,
          "右槽组合=\"时钟 电压\"", v.voltage_text);

    /* tz=+480（中国）：11:10 UTC → 19:10 同日 */
    r.tz_offset_min = 480;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.clock_text, "19:10") == 0, "tz=480 → 19:10", v.clock_text);

    /* tz=-60 → 10:10（负偏移，同日内） */
    r.tz_offset_min = -60;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.clock_text, "10:10") == 0, "tz=-60 → 10:10", v.clock_text);

    /* 无电池电压 → 时钟独占右槽 */
    r.battery_valid = false;
    r.tz_offset_min = 480;
    cdt_present(&s, &r, 40000, &v);
    check(strcmp(v.voltage_text, "19:10") == 0, "无电压 → 时钟独占右槽",
          v.voltage_text);
}

int main(void)
{
    test_priority_low_battery();
    test_priority_sleep_prep();
    test_voltage();
    test_status_words();
    test_cancelled();
    test_no_tasks_and_no_state();
    test_usage_and_plan();
    test_usage_blocks_and_plan_current();
    test_fresh_timing();
    test_duration_format();
    test_truncation();
    test_attention_and_mute();
    test_details_fields();
    test_header_clock();
    printf("\n汇总: %d PASS, %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
