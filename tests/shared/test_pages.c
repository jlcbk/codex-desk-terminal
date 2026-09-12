/*
 * test_pages.c — P2.2/P2.3 主机端 C 断言测试（A2）
 *
 * 覆盖（P2.2/P2.3 验收 + INTERFACES §4/§6）：
 *   - AGENTS：排序（needs_you→error→working/thinking→done→idle；同级
 *     updated_at 降序、id 升序）、4 行/页分页计数、总数/裁剪标记、空数据
 *   - PLAN：只数 completed、total 来自数据、分页计数、按选中任务生成、空计划
 *   - USAGE：窗口名取自数据（不编造）、pct 取整、实际窗口长度、reset 倒计时
 *     （fresh 推进/陈旧冻结/过期不猜 0%/缺失 RST --）、context 独立
 *     （token 不冒充 context）、额度缺失
 *   - P2.3 强制页：电池 critical/sleep_prep 优先于 NEEDS YOU；任意 selected_page
 *     下强制页不脱离；恢复（ACTIVE/健康链路）保留普通页面、计时解冻
 *   - 导航（cdt_nav，纯逻辑）：短按子页先推进再切主页面（P2 固定行为）、
 *     五页轮换 NOW→AGENTS→PLAN→USAGE→DETAILS→NOW（ZC4 v1.2）、
 *     长按只静音、低压强制页拒普通页切换、子页越界钳制
 * 任何 FAIL 退出码 1。
 */
#include <stdio.h>
#include <string.h>

#include "cdt_nav.h"
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

/* ---------- 基准（与 test_presenter.c 同规则） ---------- */

static cdt_thread_t mk_thread(const char *id, cdt_thread_state_t st,
                              int64_t updated_at, uint16_t plan_total)
{
    cdt_thread_t t;

    memset(&t, 0, sizeof(t));
    snprintf(t.id, sizeof(t.id), "%s", id);
    t.turn_id_present = true;
    snprintf(t.turn_id, sizeof(t.turn_id), "turn-%s", id);
    snprintf(t.project, sizeof(t.project), "proj-%s", id);
    t.state = st;
    snprintf(t.activity, sizeof(t.activity), "act %s", id);
    t.updated_at_ms_present = true;
    t.updated_at_ms = updated_at;
    t.elapsed_ms = 1000;
    t.waiting_ms = 0;
    t.end_reason = CDT_END_REASON_NULL;
    t.plan.total = plan_total;
    return t;
}

static cdt_app_state_t base_state(void)
{
    cdt_app_state_t s;

    memset(&s, 0, sizeof(s));
    snprintf(s.bridge_epoch, sizeof(s.bridge_epoch), "test-epoch");
    s.seq = 1;
    s.generated_at_ms_present = true;
    s.generated_at_ms = (int64_t)1789000000000LL + 10000; /* T0+10s */
    s.source.kind = CDT_SOURCE_MOCK;
    s.source.connected = true;
    s.source.stale = false;
    s.threads_total = 0;
    s.usage.available = false;
    return s;
}

static cdt_runtime_t base_rt(void)
{
    cdt_runtime_t r;

    memset(&r, 0, sizeof(r));
    r.battery_valid = true;
    r.battery_mv = 3900;
    r.usable_percent = 50;
    r.power_state = CDT_POWER_ACTIVE;
    snprintf(r.transport, sizeof(r.transport), "mock");
    r.link_state = CDT_LINK_CONNECTED;
    r.last_rx_monotonic_ms = 30000;
    r.selected_page = CDT_PAGE_NOW;
    return r;
}

static void add_thread(cdt_app_state_t *s, cdt_thread_t t)
{
    if (s->thread_count < CDT_MAX_THREADS) {
        s->threads[s->thread_count++] = t;
    }
    s->threads_total = s->thread_count;
}

static void add_window(cdt_app_state_t *s, const char *label, double pct,
                       bool pct_present, uint16_t mins, bool rst_present,
                       int64_t resets_at)
{
    cdt_usage_window_t *w = &s->usage.windows[s->usage.window_count++];

    memset(w, 0, sizeof(*w));
    snprintf(w->label, sizeof(w->label), "%s", label);
    w->used_percent_present = pct_present;
    w->used_percent = pct;
    w->duration_mins = mins;
    w->resets_at_ms_present = rst_present;
    w->resets_at_ms = resets_at;
    s->usage.available = true;
    s->usage.windows_total = s->usage.window_count;
}

/* ================== AGENTS：排序 / 分页 / 裁剪 ================== */

static void test_agents_sort(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* 乱序注入；两个 working 用 updated_at 区分；两个 needs_you 用 id 打破平局 */
    add_thread(&s, mk_thread("t-done", CDT_THREAD_STATE_DONE, 100, 0));
    add_thread(&s, mk_thread("t-work-b", CDT_THREAD_STATE_WORKING, 300, 0));
    add_thread(&s, mk_thread("t-ny-b", CDT_THREAD_STATE_NEEDS_YOU, 500, 0));
    add_thread(&s, mk_thread("t-idle", CDT_THREAD_STATE_IDLE, 900, 0));
    add_thread(&s, mk_thread("t-work-a", CDT_THREAD_STATE_WORKING, 400, 0));
    add_thread(&s, mk_thread("t-err", CDT_THREAD_STATE_ERROR, 600, 0));
    add_thread(&s, mk_thread("t-think", CDT_THREAD_STATE_THINKING, 350, 0));
    add_thread(&s, mk_thread("t-ny-a", CDT_THREAD_STATE_NEEDS_YOU, 500, 0));

    cdt_present(&s, &r, 30000, &v);
    check(v.agents_count == 8, "AGENTS 可见行数=8", "");
    /* 期望序：ny-a,ny-b(同级 updated_at 同 500 → id 升序)、err、
     * work-a(400)>work-b(300)、think(350)?? —— think 与 work 同级(2)，
     * updated_at: work-a 400 > think 350 > work-b 300、done、idle */
    check(v.agents_rows[0].state == CDT_THREAD_STATE_NEEDS_YOU &&
              strcmp(v.agents_rows[0].project, "proj-t-ny-a") == 0,
          "AGENTS[0]=needs_you 且 id 升序打破平局（ny-a < ny-b）", v.agents_rows[0].project);
    check(v.agents_rows[1].state == CDT_THREAD_STATE_NEEDS_YOU &&
              strcmp(v.agents_rows[1].project, "proj-t-ny-b") == 0,
          "AGENTS[1]=needs_you 第二行", v.agents_rows[1].project);
    check(v.agents_rows[2].state == CDT_THREAD_STATE_ERROR,
          "AGENTS[2]=error（优先级第二）", "");
    check(v.agents_rows[3].state == CDT_THREAD_STATE_WORKING &&
              strcmp(v.agents_rows[3].project, "proj-t-work-a") == 0,
          "AGENTS[3]=working 同级 updated_at 降序（400）", v.agents_rows[3].project);
    check(v.agents_rows[4].state == CDT_THREAD_STATE_THINKING &&
              strcmp(v.agents_rows[4].project, "proj-t-think") == 0,
          "AGENTS[4]=thinking 与 working 同级（350）", v.agents_rows[4].project);
    check(v.agents_rows[5].state == CDT_THREAD_STATE_WORKING &&
              strcmp(v.agents_rows[5].project, "proj-t-work-b") == 0,
          "AGENTS[5]=working（300）", v.agents_rows[5].project);
    check(v.agents_rows[6].state == CDT_THREAD_STATE_DONE,
          "AGENTS[6]=done", "");
    check(v.agents_rows[7].state == CDT_THREAD_STATE_IDLE,
          "AGENTS[7]=idle（最末）", "");
    check(v.agents_rows[0].waiting && v.agents_rows[0].emphasized,
          "needs_you 行 waiting 标记（等待提醒）", "");
    check(v.agents_rows[2].emphasized && !v.agents_rows[2].waiting,
          "error 行强调但非 waiting", "");
    check(strcmp(v.agents_rows[0].state_label, "NEEDS YOU") == 0,
          "状态词透传 NEEDS YOU", v.agents_rows[0].state_label);
}

static void test_agents_paging_and_hidden(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    int i;

    for (i = 0; i < 8; i++) {
        char id[8];
        snprintf(id, sizeof(id), "t%d", i);
        add_thread(&s, mk_thread(id, CDT_THREAD_STATE_WORKING, i, 0));
    }
    s.threads_total = 10; /* 桥端裁剪：8 可见 / 共 10 */
    s.threads_truncated = true;

    cdt_present(&s, &r, 30000, &v);
    check(v.agents_pages == 2, "8 行 → 2 页（4 行/页）", "");
    check(v.agents_hidden == 2 && v.threads_truncated,
          "裁剪标记 hidden=2（还有 N 个）", "");
}

static void test_agents_empty(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    cdt_present(&s, &r, 30000, &v);
    check(v.agents_count == 0 && v.agents_pages == 1 && v.agents_hidden == 0,
          "空数据 → AGENTS 0 行单页（UI 空态）", "");
}

/* ================== PLAN：计数 / 分页 / 选中 ================== */

static void fill_plan(cdt_thread_t *t, uint16_t total, int n_steps, int n_done)
{
    int i;

    t->plan.total = total;
    t->plan.truncated = (total > (uint16_t)n_steps);
    t->plan.step_count = (uint8_t)n_steps;
    for (i = 0; i < n_steps; i++) {
        snprintf(t->plan.steps[i].text, sizeof(t->plan.steps[i].text), "step-%d", i);
        t->plan.steps[i].status = (i < n_done) ? (uint8_t)CDT_STEP_STATUS_COMPLETED
                                               : (uint8_t)CDT_STEP_STATUS_IN_PROGRESS;
    }
}

static void test_plan_counts_and_paging(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t t = mk_thread("t1", CDT_THREAD_STATE_WORKING, 100, 0);

    fill_plan(&t, 14, 8, 3); /* 3 done、1 in_progress、4 pending；原始 14 步 */
    add_thread(&s, t);

    cdt_present(&s, &r, 30000, &v);
    check(v.plan_completed == 3, "只数 completed（in_progress 不计）", "");
    check(v.plan_total == 14 && v.plan_truncated, "total=14 来自数据且截断标记透传", "");
    check(v.plan_step_count == 8 && v.plan_pages == 2, "8 可见步骤 → 2 子页", "");
    check(strcmp(v.plan_steps[3].text, "step-3") == 0 &&
              v.plan_steps[3].status == CDT_STEP_STATUS_IN_PROGRESS,
          "步骤原始顺序保留", "");
    check(v.plan_present && strcmp(v.plan_text, "PLAN 3/14") == 0,
          "NOW 摘要与 PLAN 页共用同一计数 PLAN 3/14", v.plan_text);
}

static void test_plan_follows_selected(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t a = mk_thread("t-a", CDT_THREAD_STATE_WORKING, 100, 0);
    cdt_thread_t b = mk_thread("t-b", CDT_THREAD_STATE_WORKING, 200, 0);

    fill_plan(&a, 2, 2, 0);
    fill_plan(&b, 5, 5, 5);
    add_thread(&s, a);
    add_thread(&s, b);

    /* 无选中 → 默认首项（桥端排序后的 threads[0]） */
    cdt_present(&s, &r, 30000, &v);
    check(v.plan_total == 2, "无选中 → PLAN 取 threads[0]", "");

    /* 选中 t-b → PLAN 按选中任务生成 */
    s.selected_thread_id_present = true;
    snprintf(s.selected_thread_id, sizeof(s.selected_thread_id), "t-b");
    cdt_present(&s, &r, 30000, &v);
    check(v.plan_total == 5 && v.plan_completed == 5,
          "选中 t-b → PLAN 按选中任务生成", "");
}

static void test_plan_empty(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    add_thread(&s, mk_thread("t1", CDT_THREAD_STATE_WORKING, 100, 0));
    cdt_present(&s, &r, 30000, &v);
    check(v.plan_total == 0 && v.plan_step_count == 0 && v.plan_pages == 1,
          "无计划 → total=0、0 步、单页（UI 显示暂无计划）", "");
    check(!v.plan_present, "无计划 → NOW 摘要行隐藏", "");
}

/* ================== USAGE：窗口 / 倒计时 / context ================== */

static void test_usage_windows(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* 窗口名来自数据：即使非常规命名也原样透传（不编造 5h/周等） */
    add_window(&s, "QUARTERLY-37", 42.4, true, 300, true, 1789000000000LL + 10000 + 3600000);
    add_window(&s, "LONG WINDOW", 7.5, true, 10080, false, 0);
    cdt_present(&s, &r, 30000, &v); /* last_rx=30000=now → 无增量，fresh */

    check(v.usage_count == 2, "2 窗口可见", "");
    check(strcmp(v.usage_rows[0].label, "QUARTERLY-37") == 0,
          "窗口名取自数据（实际命名）", v.usage_rows[0].label);
    check(v.usage_rows[0].pct == 42, "42.4 → 42（取整）", "");
    check(v.usage_rows[0].duration_mins == 300, "窗口长度 300min 来自数据", "");
    check(v.usage_rows[0].reset_present && v.usage_rows[0].reset_in_s == 3600,
          "reset 倒计时 = resets_at - 业务now = 3600s", "");
    check(!v.usage_rows[1].reset_present, "resets_at 缺失 → RST --（reset_present=false）", "");
    check(v.usage_rows[1].pct == 8, "7.5 → 8（四舍五入）", "");

    /* fresh 增量：now=last_rx+60s → 倒计时同步减 60s */
    cdt_present(&s, &r, 90000, &v);
    check(v.usage_rows[0].reset_in_s == 3540, "fresh：倒计时随单调增量推进 3540s", "");

    /* 陈旧冻结：link stale → 倒计时停住 */
    r.link_state = CDT_LINK_STALE;
    cdt_present(&s, &r, 200000, &v);
    check(v.usage_rows[0].reset_in_s == 3600 && v.time_frozen,
          "陈旧：倒计时冻结在业务基值", "");
    r.link_state = CDT_LINK_CONNECTED;
    s.source.stale = true;
    cdt_present(&s, &r, 200000, &v);
    check(v.usage_rows[0].reset_in_s == 3600, "source.stale：倒计时同样冻结", "");
    s.source.stale = false;
}

static void test_usage_expired_and_edge(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;

    /* 已过 reset（resets_at 早于业务 now）：不猜 0%，pct 保持数据值 */
    add_window(&s, "PAST WINDOW", 55.0, true, 60, true, 1789000000000LL);
    /* 4 窗口顶格 */
    add_window(&s, "W2", 0.0, true, 5, false, 0);
    add_window(&s, "W3", 100.0, true, 1440, false, 0);
    add_window(&s, "W4", 1.0, true, 1, false, 0);

    cdt_present(&s, &r, 30000, &v);
    check(v.usage_count == 4, "4 窗口顶格全部可见", "");
    check(v.usage_rows[0].reset_in_s < 0 && v.usage_rows[0].pct_present &&
              v.usage_rows[0].pct == 55,
          "已过 reset → EXPIRED 且 pct 保持 55（不猜 0%）", "");
    check(v.usage_rows[2].pct == 100, "100% 顶格保留", "");
    check(v.usage_rows[1].pct_present && v.usage_rows[1].pct == 0,
          "0% 合法保留（数据为 0 即 0）", "");
}

static void test_usage_missing_and_context(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t t = mk_thread("t1", CDT_THREAD_STATE_WORKING, 100, 0);

    add_thread(&s, t);
    cdt_present(&s, &r, 30000, &v);
    check(v.usage_count == 0 && strcmp(v.usage_text, "--") == 0,
          "额度缺失 → 0 窗口、NOW 摘要 --", v.usage_text);
    check(strcmp(v.context_text, "CTX --") == 0,
          "context 无百分比 → CTX --", v.context_text);

    s.threads[0].context.used_percent_present = true;
    s.threads[0].context.used_percent = 37.6;
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.context_text, "CTX 38%") == 0, "context 百分比 → CTX 38%", v.context_text);

    /* 只有累计 token（无百分比）：不得冒充 context 占用 */
    s.threads[0].context.used_percent_present = false;
    s.threads[0].context.used_tokens_present = true;
    s.threads[0].context.used_tokens = 123000;
    cdt_present(&s, &r, 30000, &v);
    check(strcmp(v.context_text, "CTX --") == 0,
          "仅 token 总量 → 仍 CTX --（不冒充 context）", v.context_text);
}

/* ================== P2.3：强制页 / 恢复顺序 ================== */

static void test_forced_page_priority(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t t = mk_thread("t1", CDT_THREAD_STATE_NEEDS_YOU, 100, 0);

    t.attention_present = true;
    t.attention.pending_count = 1;
    snprintf(t.attention.summary, sizeof(t.attention.summary), "approve?");
    add_thread(&s, t);
    r.selected_page = CDT_PAGE_AGENTS;

    r.power_state = CDT_POWER_CRITICAL;
    cdt_present(&s, &r, 30000, &v);
    check(v.page == CDT_PAGE_LOW_BATTERY && v.low_battery_forced,
          "CRITICAL 强制 LOW BATTERY 页（任意 selected_page 下）", "");
    check(strcmp(v.status_label, "LOW BATTERY") == 0,
          "电池优先于 NEEDS YOU（状态词覆盖）", v.status_label);
    check(strcmp(v.voltage_text, "3.90V") == 0,
          "强制页仍显示电压（§6 低压页含电压）", v.voltage_text);

    r.power_state = CDT_POWER_SLEEP_PREP;
    cdt_present(&s, &r, 30000, &v);
    check(v.low_battery_forced, "SLEEP_PREP 同样强制", "");
}

static void test_recovery_order(void)
{
    cdt_app_state_t s = base_state();
    cdt_runtime_t r = base_rt();
    cdt_view_t v;
    cdt_thread_t t = mk_thread("t1", CDT_THREAD_STATE_WORKING, 100, 0);

    add_thread(&s, t);
    r.selected_page = CDT_PAGE_PLAN;

    /* 断连：冻结 + 提示位，业务页保留 */
    r.link_state = CDT_LINK_DISCONNECTED;
    cdt_present(&s, &r, 30000, &v);
    check(v.link_disconnected && v.time_frozen && v.page == CDT_PAGE_PLAN,
          "断连 → 冻结提示且业务页保留（不回退）", "");

    /* 恢复健康连接：保留当前普通页面，计时解冻 */
    r.link_state = CDT_LINK_CONNECTED;
    cdt_present(&s, &r, 40000, &v);
    check(!v.link_disconnected && !v.link_stale && !v.time_frozen &&
              v.page == CDT_PAGE_PLAN && strcmp(v.elapsed_text, "00:11") == 0,
          "恢复 → 页面保留、提示消失、计时推进（00:10+1s）", v.elapsed_text);

    /* 低压强制 → 解除：回到恢复后的普通页（深睡重启才回 NOW，由宿主置位） */
    r.power_state = CDT_POWER_CRITICAL;
    cdt_present(&s, &r, 40000, &v);
    check(v.page == CDT_PAGE_LOW_BATTERY, "低压期间强制页", "");
    r.power_state = CDT_POWER_ACTIVE;
    cdt_present(&s, &r, 40000, &v);
    check(!v.low_battery_forced && v.page == CDT_PAGE_PLAN,
          "低压解除 → 回到 nav 普通页（保留 PLAN）", "");

    /* stale → fresh 同规则 */
    r.link_state = CDT_LINK_STALE;
    cdt_present(&s, &r, 40000, &v);
    check(v.link_stale && v.page == CDT_PAGE_PLAN, "stale 提示独立于业务状态", "");
    r.link_state = CDT_LINK_CONNECTED;
    cdt_present(&s, &r, 40000, &v);
    check(!v.link_stale && v.page == CDT_PAGE_PLAN, "stale 解除 → 页面不变", "");
}

/* ================== 导航：cdt_nav（纯逻辑） ================== */

static cdt_view_t nav_view(bool forced, uint8_t agents_pages, uint8_t plan_pages)
{
    cdt_view_t v;

    memset(&v, 0, sizeof(v));
    v.low_battery_forced = forced;
    v.agents_pages = agents_pages;
    v.plan_pages = plan_pages;
    return v;
}

static void test_nav_cycle(void)
{
    cdt_nav_t nav;
    cdt_view_t v = nav_view(false, 1, 1);
    uint32_t act;

    cdt_nav_init(&nav, CDT_PAGE_NOW);
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(nav.page == CDT_PAGE_AGENTS && (act & CDT_NAV_ACT_PAGE),
          "短按 NOW→AGENTS", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(nav.page == CDT_PAGE_PLAN && (act & CDT_NAV_ACT_PAGE),
          "单页 AGENTS 短按直接切 PLAN", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(nav.page == CDT_PAGE_USAGE, "短按 PLAN→USAGE", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(nav.page == CDT_PAGE_DETAILS, "短按 USAGE→DETAILS（ZC4 五页循环）", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(nav.page == CDT_PAGE_NOW, "短按 DETAILS→NOW（轮换闭环）", "");
}

static void test_nav_details(void)
{
    /* ZC4 v1.2：DETAILS 为合法普通页——init 接受、长按只静音、clamp 不归一。 */
    cdt_nav_t nav;
    cdt_view_t v = nav_view(false, 1, 1);
    uint32_t act;

    cdt_nav_init(&nav, CDT_PAGE_DETAILS);
    check(nav.page == CDT_PAGE_DETAILS, "init(Details) 接受为普通页起点", "");

    act = cdt_nav_key(&nav, &v, CDT_KEY_LONG_PRESS);
    check((act & CDT_NAV_ACT_MUTE) && !(act & CDT_NAV_ACT_PAGE) &&
              nav.page == CDT_PAGE_DETAILS,
          "DETAILS 长按只静音不切页", "");

    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check((act & CDT_NAV_ACT_PAGE) && nav.page == CDT_PAGE_NOW,
          "DETAILS 短按回 NOW", "");

    nav.page = CDT_PAGE_DETAILS;
    check(!cdt_nav_clamp(&nav, &v) && nav.page == CDT_PAGE_DETAILS,
          "clamp 不把 DETAILS 归一（合法普通页）", "");
}

static void test_nav_subpage_first(void)
{
    cdt_nav_t nav;
    cdt_view_t v = nav_view(false, 2, 3); /* AGENTS 2 页、PLAN 3 页 */
    uint32_t act;

    cdt_nav_init(&nav, CDT_PAGE_AGENTS);
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(act == CDT_NAV_ACT_NONE && nav.page == CDT_PAGE_AGENTS &&
              nav.agents_page == 1,
          "AGENTS 有子页：短按先推进子页（1/2）", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check((act & CDT_NAV_ACT_PAGE) && nav.page == CDT_PAGE_PLAN && nav.plan_page == 0,
          "AGENTS 末子页再按 → 切 PLAN 且子页清零", "");

    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(act == CDT_NAV_ACT_NONE && nav.plan_page == 1, "PLAN 子页 1/3", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check(act == CDT_NAV_ACT_NONE && nav.plan_page == 2, "PLAN 子页 2/3", "");
    act = cdt_nav_key(&nav, &v, CDT_KEY_SHORT_PRESS);
    check((act & CDT_NAV_ACT_PAGE) && nav.page == CDT_PAGE_USAGE,
          "PLAN 末子页再按 → 切 USAGE", "");
}

static void test_nav_forced_and_mute(void)
{
    cdt_nav_t nav;
    cdt_view_t forced = nav_view(true, 2, 2);
    cdt_view_t normal = nav_view(false, 2, 2);
    uint32_t act;

    cdt_nav_init(&nav, CDT_PAGE_AGENTS);
    nav.agents_page = 1;

    act = cdt_nav_key(&nav, &forced, CDT_KEY_SHORT_PRESS);
    check(act == CDT_NAV_ACT_NONE && nav.page == CDT_PAGE_AGENTS &&
              nav.agents_page == 1,
          "低压强制页：短按被拒（页面/子页都不变）", "");

    act = cdt_nav_key(&nav, &forced, CDT_KEY_LONG_PRESS);
    check((act & CDT_NAV_ACT_MUTE) && !(act & CDT_NAV_ACT_PAGE) &&
              nav.page == CDT_PAGE_AGENTS,
          "低压强制页：长按只静音，不解除强制页", "");

    act = cdt_nav_key(&nav, &normal, CDT_KEY_LONG_PRESS);
    check((act & CDT_NAV_ACT_MUTE) && !(act & CDT_NAV_ACT_PAGE) &&
              nav.page == CDT_PAGE_AGENTS,
          "普通页长按：只静音不切页", "");
}

static void test_nav_clamp(void)
{
    cdt_nav_t nav;
    cdt_view_t v = nav_view(false, 2, 2);
    cdt_view_t shrunk = nav_view(false, 1, 0);

    cdt_nav_init(&nav, CDT_PAGE_AGENTS);
    nav.agents_page = 1;
    nav.plan_page = 1;
    check(!cdt_nav_clamp(&nav, &v) && nav.agents_page == 1 && nav.plan_page == 1,
          "有效子页 clamp 不改动（返回 false）", "");
    check(cdt_nav_clamp(&nav, &shrunk) && nav.agents_page == 0 && nav.plan_page == 0,
          "内容收缩 → 子页钳制回 0（返回 true）", "");
    cdt_nav_init(&nav, CDT_PAGE_NOW);
    nav.page = CDT_PAGE_LOW_BATTERY; /* 模拟异常值混入（绕过 init 归一） */
    check(cdt_nav_clamp(&nav, &v) && nav.page == CDT_PAGE_NOW,
          "强制页/异常页混入 nav → 归一 NOW（防御）", "");
}

int main(void)
{
    test_agents_sort();
    test_agents_paging_and_hidden();
    test_agents_empty();
    test_plan_counts_and_paging();
    test_plan_follows_selected();
    test_plan_empty();
    test_usage_windows();
    test_usage_expired_and_edge();
    test_usage_missing_and_context();
    test_forced_page_priority();
    test_recovery_order();
    test_nav_cycle();
    test_nav_details();
    test_nav_subpage_first();
    test_nav_forced_and_mute();
    test_nav_clamp();
    printf("\n汇总: %d PASS, %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
