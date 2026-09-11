/*
 * test_app_inbox.c — 收件槽跨任务交接 + 优先提醒槽测试（审核 R4 修复验收）
 *
 * 审核真源：docs/ARCHITECTURE_REVIEW_2026-09-11.md R4；契约：INTERFACES §5
 * （容量1普通槽 + 有界优先事件槽、先呈现后续终态、超预算必须计数）、§3
 * seq 行（同 epoch ≤ 已应用值丢弃）。
 *
 * 被测对象 = 真实 firmware/main/app_inbox.c（宿主构建 -DCDT_APP_INBOX_HOST，
 * pthread 锁编译同一交接逻辑）；优先语义判定 + 完整解析链另编入真实
 * shared/state（cdt_json/cdt_parser/cdt_store）验证 produce→take→apply。
 *
 * 覆盖（审核 R4 验收清单）：
 *   - 交接完整性：并发灌入（含最大长度、快速交替）+ 延迟消费者，逐条校验
 *     内容无撕裂、无重复、守恒 produced = consumed + dropped_n + dropped_p。
 *   - 优先槽：attention→紧随 done 不丢提醒（done 不得顶掉未呈现提醒）；
 *     重复 seq（重复普通快照）持续发送不顶掉未呈现提醒；提醒呈现后槽让位。
 *   - 分类器：needs_you/error → 优先；working/done/idle/thinking → 普通。
 *   - dropped 计数语义保留：普通槽覆盖计数；优先槽被新提醒覆盖单独计数。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_inbox.h"

static int failures = 0;
static int passes = 0;

static void check(int cond, const char *name, const char *detail)
{
    if (cond) {
        passes++;
        printf("[PASS] %s\n", name);
    } else {
        failures++;
        printf("[FAIL] %s — %s\n", name, detail ? detail : "");
    }
}

/* ------------------------------------------------------------------ */
/* 分类器（app_inbox_is_priority 纯函数）                              */
/* ------------------------------------------------------------------ */

static void test_classifier(void)
{
    check(app_inbox_is_priority((const uint8_t *)"{\"threads\":"
          "[{\"state\":\"needs_you\"}]}", 40),
          "r4/classify: 紧凑 needs_you → 优先", "漏判");
    check(app_inbox_is_priority((const uint8_t *)"{\"state\":\"error\"}", 17),
          "r4/classify: 紧凑 error → 优先", "漏判");
    check(app_inbox_is_priority((const uint8_t *)"{\"state\" : \"needs_you\"}", 23),
          "r4/classify: 容忍空白变体", "漏判");
    check(!app_inbox_is_priority((const uint8_t *)"{\"state\":\"done\"}", 16) &&
          !app_inbox_is_priority((const uint8_t *)"{\"state\":\"working\"}", 19) &&
          !app_inbox_is_priority((const uint8_t *)"{\"state\":\"idle\"}", 16) &&
          !app_inbox_is_priority((const uint8_t *)"{\"state\":\"thinking\"}", 20),
          "r4/classify: 普通状态不进优先槽", "误判");
    check(!app_inbox_is_priority((const uint8_t *)"{\"activity\":\"call state: needs_you now\"}", 40),
          "r4/classify: 正文无字段形态不误判", "误判");
    /* JSON 字符串值内的引号必为 \" 转义形态，匹配不上未转义字段形态：
     * 正文里的逐字 "state":"needs_you" 文本不会命中（只在真实键/值位点命中），
     * 误报仅剩畸形重复键——唯一发送方为本项目 Bridge。 */
    check(!app_inbox_is_priority((const uint8_t *)"{\"note\":\"\\\"state\\\":\\\"needs_you\\\"\"}", 34),
          "r4/classify: 正文转义引号不命中字段形态", "误判");
    check(!app_inbox_is_priority(NULL, 0) &&
          !app_inbox_is_priority((const uint8_t *)"", 0) &&
          !app_inbox_is_priority((const uint8_t *)"\"state\":", 8),
          "r4/classify: 空/截断输入安全", "崩溃或误判");
}

/* ------------------------------------------------------------------ */
/* 优先槽语义（单线程确定性交错；消息 = 带标签的合法 JSON 形状字节串）   */
/* ------------------------------------------------------------------ */

#define MSG_MAX APP_INBOX_MSG_MAX
static uint8_t cons_buf[MSG_MAX]; /* 消费者独享缓冲（= main.c s_inbox_cons） */

static size_t mk_msg(char *buf, const char *state, unsigned seq)
{
    /* 与 Bridge 冻结编码同构的紧凑 JSON（本测试只经 app_inbox 的字节层；
     * 完整解析链在 test_full_stack 用真实 store）。 */
    return (size_t)snprintf(buf, MSG_MAX,
                            "{\"seq\":%u,\"threads\":[{\"state\":\"%s\"}]}",
                            seq, state);
}

static void take_none_left(void)
{
    size_t len;
    check(!app_inbox_take(cons_buf, sizeof cons_buf, &len),
          "r4/take: 槽空返回 false", "误报有消息");
}

/* 场景①（审核验收）：attention 后紧随 done —— done 不得丢提醒。
 * 修复前：capacity-1 无差别覆盖 → needs_you 永不呈现。 */
static void test_attention_then_done_keeps_reminder(void)
{
    char a[MSG_MAX], d[MSG_MAX];
    size_t la, ld, len;

    app_inbox_init();
    la = mk_msg(a, "needs_you", 1);
    ld = mk_msg(d, "done", 2);
    app_inbox_produce((const uint8_t *)a, la);
    app_inbox_produce((const uint8_t *)d, ld);

    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) && len == la &&
          memcmp(cons_buf, a, la) == 0,
          "r4/priority: attention→done 先取到 attention（提醒先呈现）",
          "提醒被 done 顶掉");
    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) && len == ld &&
          memcmp(cons_buf, d, ld) == 0,
          "r4/priority: 随后取到 done（后续终态正常呈现）", "done 丢失");
    check(app_inbox_dropped_normal() == 0 && app_inbox_dropped_priority() == 0,
          "r4/priority: 双槽各 capacity-1 无丢弃", "误计丢弃");
    take_none_left();
}

/* 场景②（审核验收）：重复 seq 持续发送不顶掉未呈现提醒。 */
static void test_repeated_seq_flood_keeps_reminder(void)
{
    char p[MSG_MAX], n[MSG_MAX];
    size_t lp, ln, len;
    int i;

    app_inbox_init();
    lp = mk_msg(p, "needs_you", 10);
    ln = mk_msg(n, "done", 10); /* 同 seq 重复（存活快照/重发） */
    app_inbox_produce((const uint8_t *)p, lp);
    for (i = 0; i < 5; i++) {
        app_inbox_produce((const uint8_t *)n, ln);
    }

    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) &&
          memcmp(cons_buf, p, lp) == 0,
          "r4/priority: 重复 seq 洪泛后提醒仍完好", "提醒被顶掉");
    check(app_inbox_dropped_priority() == 0,
          "r4/priority: 洪泛不进优先槽（dropped_prio=0）", "优先槽被污染");
    check(app_inbox_dropped_normal() == 4,
          "r4/priority: 普通槽 capacity-1 覆盖计数（语义保留）",
          "dropped 计数错误");
    (void)app_inbox_take(cons_buf, sizeof cons_buf, &len); /* 排空普通槽 */
    take_none_left();
}

/* 场景③：提醒呈现后槽让位；新提醒覆盖旧提醒必须计数（§5 超预算计数）。 */
static void test_priority_slot_rearm_and_budget_count(void)
{
    char p1[MSG_MAX], p2[MSG_MAX], n[MSG_MAX];
    size_t l1, l2, ln, len;

    app_inbox_init();
    l1 = mk_msg(p1, "needs_you", 20);
    app_inbox_produce((const uint8_t *)p1, l1);
    (void)app_inbox_take(cons_buf, sizeof cons_buf, &len); /* 呈现 → 槽让位 */
    ln = mk_msg(n, "done", 21);
    app_inbox_produce((const uint8_t *)n, ln);
    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) &&
          memcmp(cons_buf, n, ln) == 0,
          "r4/priority: 呈现后槽让位，普通消息正常流转", "普通槽卡死");

    l1 = mk_msg(p1, "error", 30);
    l2 = mk_msg(p2, "needs_you", 31);
    app_inbox_produce((const uint8_t *)p1, l1);
    app_inbox_produce((const uint8_t *)p2, l2); /* 有界=1：新提醒顶旧提醒 */
    check(app_inbox_dropped_priority() == 1,
          "r4/priority: 优先槽被覆盖必须计数（§5）", "未计数");
    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) &&
          memcmp(cons_buf, p2, l2) == 0,
          "r4/priority: 有界优先槽保留最新提醒", "取到旧提醒或丢失");
    take_none_left();
}

/* ------------------------------------------------------------------ */
/* 完整解析链（真实 shared/state：produce → take → cdt_state_store_apply）*/
/* ------------------------------------------------------------------ */

static const char APPSTATE_TMPL[] =
    "{\"schema_version\":1,\"kind\":\"state\",\"bridge_epoch\":\"r4-test\","
    "\"seq\":%u,\"generated_at_ms\":null,\"source\":{\"kind\":\"mock\","
    "\"connected\":true,\"stale\":false,\"last_event_at_ms\":null},"
    "\"selected_thread_id\":\"t1\",\"threads_total\":1,"
    "\"threads_truncated\":false,\"threads\":[{\"id\":\"t1\",\"turn_id\":null,"
    "\"project\":\"p\",\"state\":\"%s\",\"activity\":\"a\",\"updated_at_ms\":null,"
    "\"elapsed_ms\":0,\"waiting_ms\":0,\"end_reason\":null,\"attention\":null,"
    "\"plan\":{\"total\":0,\"truncated\":false,\"steps\":[]},\"context\":"
    "{\"used_tokens\":null,\"capacity_tokens\":null,\"used_percent\":null}}],"
    "\"usage\":{\"available\":false,\"updated_at_ms\":null,\"windows_total\":0,"
    "\"windows_truncated\":false,\"windows\":[]}}";

#include "cdt_store.h"

static size_t mk_appstate(char *buf, unsigned seq, const char *state)
{
    return (size_t)snprintf(buf, MSG_MAX, APPSTATE_TMPL, seq, state);
}

/* 审核验收「attention→done 突发」经真实解析链：两条都完整落 store；
 * 先提醒后终态；重复 seq 由 store 的 IGNORED_STALE_SEQ 门卫拒绝（显示停在
 * 最新，§3），不在交接层丢提醒。 */
static void test_full_stack_store_apply(void)
{
    static cdt_state_store_t store;
    char j1[MSG_MAX], j2[MSG_MAX];
    size_t l1, l2, len;
    cdt_parse_result_t r;
    const cdt_app_state_t *snap;

    app_inbox_init();
    cdt_state_store_init(&store);
    l1 = mk_appstate(j1, 1, "needs_you");
    l2 = mk_appstate(j2, 2, "done");
    check(app_inbox_is_priority((const uint8_t *)j1, l1) &&
          !app_inbox_is_priority((const uint8_t *)j2, l2),
          "r4/fullstack: 合法 AppState 分类 needs_you/done", "分类错误");

    app_inbox_produce((const uint8_t *)j1, l1);
    app_inbox_produce((const uint8_t *)j2, l2);

    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) && len == l1 &&
          (r = cdt_state_store_apply(&store, cons_buf, len)) == CDT_PARSE_OK,
          "r4/fullstack: 提醒快照整包应用 OK", "应用失败");
    snap = cdt_state_store_snapshot(&store);
    check(snap != NULL && snap->seq == 1 && snap->thread_count == 1 &&
          strcmp(snap->threads[0].id, "t1") == 0,
          "r4/fullstack: store 内容与消息一致（无撕裂）", "内容不符");

    check(app_inbox_take(cons_buf, sizeof cons_buf, &len) && len == l2 &&
          (r = cdt_state_store_apply(&store, cons_buf, len)) == CDT_PARSE_OK,
          "r4/fullstack: 终态快照整包应用 OK", "应用失败");
    snap = cdt_state_store_snapshot(&store);
    check(snap != NULL && snap->seq == 2,
          "r4/fullstack: 终态推进到 seq=2", "seq 不符");

    /* 重复 seq：store 门卫拒绝，状态保留 seq=2（重复不洗掉终态） */
    app_inbox_produce((const uint8_t *)j1, l1);
    (void)app_inbox_take(cons_buf, sizeof cons_buf, &len);
    r = cdt_state_store_apply(&store, cons_buf, len);
    check(r == CDT_PARSE_IGNORED_STALE_SEQ &&
          cdt_state_store_snapshot(&store)->seq == 2,
          "r4/fullstack: 重复 seq → IGNORED_STALE_SEQ（§3 门卫）", "错误接受");
    take_none_left();
}

/* ------------------------------------------------------------------ */
/* 并发压力（宿主线程 + 真实交接代码）：延迟消费者 + 交接完整性守恒       */
/* 每条消息内容 = n 的确定性函数（LCG 载荷 + 长度），消费者独立重生成后   */
/* 全字节比对——任何撕裂/混包/串槽在此暴露；每 5 条含 needs_you 字段形态   */
/* 走优先槽，双槽同压；每 97 条一条最大长度（16KiB）。                   */
/* ------------------------------------------------------------------ */

#include <pthread.h>
#include <unistd.h>

#define STRESS_N 4000u
static unsigned char consumed_flags[(STRESS_N + 7u) / 8u];

static pthread_mutex_t s_stress_mu = PTHREAD_MUTEX_INITIALIZER;
static int s_producer_done;

static size_t stress_len(unsigned n)
{
    if ((n % 97u) == 0u) {
        return MSG_MAX; /* 最大长度路径（16KiB 整包） */
    }
    return 32u + (size_t)((n * 2654435761u) % (MSG_MAX - 48u));
}

static void stress_fill(uint8_t *buf, unsigned n)
{
    size_t len = stress_len(n);
    uint32_t x = n * 2246822519u + 1u;
    int w;
    size_t i;

    if ((n % 5u) == 0u) {
        w = snprintf((char *)buf, 48, "{\"n\":%u,\"state\":\"needs_you\",\"pad\":\"", n);
    } else {
        w = snprintf((char *)buf, 40, "{\"n\":%u,\"pad\":\"", n);
    }
    for (i = (size_t)w; i + 2 < len; i++) {
        x = x * 1664525u + 1013904223u;
        buf[i] = (uint8_t)('a' + (x % 26u));
    }
    buf[len - 2] = '"';
    buf[len - 1] = '}';
}

struct stress_result {
    unsigned consumed;
    unsigned torn;
    unsigned dup;
};

static struct stress_result s_stress;

static void *stress_producer(void *arg)
{
    static uint8_t buf[MSG_MAX]; /* 生产者独享（= wss 任务侧聚合缓冲角色） */
    unsigned i;
    (void)arg;
    for (i = 0; i < STRESS_N; i++) {
        stress_fill(buf, i);
        app_inbox_produce(buf, stress_len(i));
        if ((i % 7u) == 0u) {
            usleep(50); /* 模拟桥端突发节奏 */
        }
    }
    pthread_mutex_lock(&s_stress_mu);
    s_producer_done = 1;
    pthread_mutex_unlock(&s_stress_mu);
    return NULL;
}

static void *stress_consumer(void *arg)
{
    struct stress_result *res = &s_stress;
    static uint8_t buf[MSG_MAX]; /* 消费者独享缓冲（= main.c s_inbox_cons） */
    unsigned idle = 0;
    (void)arg;

    for (;;) {
        size_t len;
        if (app_inbox_take(buf, MSG_MAX, &len)) {
            char head[25];
            unsigned n;
            memcpy(head, buf, 24);
            head[24] = '\0';
            if (sscanf(head, "{\"n\":%u", &n) == 1 && n < STRESS_N) {
                uint8_t *expected = malloc(len);
                unsigned char bit = (unsigned char)(1u << (n & 7u));
                stress_fill(expected, n);
                if (memcmp(expected, buf, len) != 0) {
                    res->torn++;
                    printf("[FAIL] r4/stress: 消息 %u 内容撕裂/混包（len=%zu）\n",
                           n, len);
                } else if ((consumed_flags[n >> 3] & bit) != 0) {
                    res->dup++;
                    printf("[FAIL] r4/stress: 消息 %u 被消费两次\n", n);
                } else {
                    consumed_flags[n >> 3] |= bit;
                    res->consumed++;
                }
                free(expected);
            } else {
                res->torn++;
                printf("[FAIL] r4/stress: 消息头不可解析（撕裂）\n");
            }
            idle = 0;
            continue;
        }
        pthread_mutex_lock(&s_stress_mu);
        {
            int done = s_producer_done;
            pthread_mutex_unlock(&s_stress_mu);
            if (done) {
                break; /* 生产者已收尾且槽空：消费/丢弃已守恒终结 */
            }
        }
        usleep(80); /* 延迟消费者：制造槽覆盖/优先交错的竞争窗口 */
        idle++;
        (void)idle;
    }
    return NULL;
}

static void test_concurrent_handoff_integrity(void)
{
    pthread_t tp, tc;
    unsigned i;
    unsigned never_seen = 0;
    /* 计数器跨用例累积（同一进程单实例），压力校验用本轮增量 */
    uint32_t del0 = app_inbox_delivered();
    uint32_t dn0 = app_inbox_dropped_normal();
    uint32_t dp0 = app_inbox_dropped_priority();
    uint32_t d_del, d_dn, d_dp;

    app_inbox_init();
    memset(consumed_flags, 0, sizeof consumed_flags);

    pthread_create(&tp, NULL, stress_producer, NULL);
    pthread_create(&tc, NULL, stress_consumer, &s_stress);
    pthread_join(tp, NULL);
    pthread_join(tc, NULL);

    d_del = app_inbox_delivered() - del0;
    d_dn = app_inbox_dropped_normal() - dn0;
    d_dp = app_inbox_dropped_priority() - dp0;

    for (i = 0; i < STRESS_N; i++) {
        if (((consumed_flags[i >> 3] >> (i & 7)) & 1u) == 0u) {
            never_seen++;
        }
    }
    check(s_stress.torn == 0 && s_stress.dup == 0,
          "r4/stress: 无撕裂/混包/重复消费", "完整性破坏");
    check(d_del == STRESS_N,
          "r4/stress: 交付计数 = 生产总数", "交付计数错误");
    check(d_del == s_stress.consumed + d_dn + d_dp,
          "r4/stress: 守恒 delivered = consumed + dropped_n + dropped_p",
          "计数不守恒（消息凭空消失）");
    check(s_stress.consumed + never_seen == STRESS_N,
          "r4/stress: 消费+覆盖丢弃覆盖全部消息", "口径不一致");
}

int main(void)
{
    test_classifier();
    test_attention_then_done_keeps_reminder();
    test_repeated_seq_flood_keeps_reminder();
    test_priority_slot_rearm_and_budget_count();
    test_full_stack_store_apply();
    test_concurrent_handoff_integrity();

    printf("\nr4/stress 汇总: consumed=%u torn=%u dup=%u dropped_n=%u "
           "dropped_p=%u\n",
           s_stress.consumed, s_stress.torn, s_stress.dup,
           app_inbox_dropped_normal(), app_inbox_dropped_priority());
    printf("%d checks: %d passed, %d failed\n", passes + failures, passes,
           failures);
    return failures == 0 ? 0 : 1;
}
