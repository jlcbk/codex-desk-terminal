/*
 * test_main.c — P1.4 共享模块主机端测试（A0）
 *
 * 用法：test_shared [fixture.json ...] [.bin ...]
 *   valid_*.json   → 期望 store_apply == CDT_PARSE_OK
 *   invalid_*.json → 期望 ERR_*（按文件名子串映射特定错误码）
 *   *.bin          → 原始字节（非法 UTF-8 用例）
 * 另含内置用例：seq/epoch 语义、拒绝后保留旧状态、帧位序。
 * 任何 FAIL 退出码 1。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cdt_frame.h"
#include "cdt_parser.h"
#include "cdt_store.h"

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

static unsigned char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    unsigned char *buf;
    long n;
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = malloc((size_t)n + 1);
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) {
        free(buf);
        fclose(f);
        return NULL;
    }
    fclose(f);
    *len = (size_t)n;
    return buf;
}

static cdt_parse_result_t expect_code_for(const char *base)
{
    if (strstr(base, "version")) {
        return CDT_PARSE_ERR_VERSION;
    }
    if (strstr(base, "utf8")) {
        return CDT_PARSE_ERR_TRUNCATED_CP;
    }
    if (strstr(base, "oversize")) {
        return CDT_PARSE_ERR_SIZE;
    }
    if (strstr(base, "nested_depth")) {
        return CDT_PARSE_ERR_DEPTH;
    }
    if (strstr(base, "string_overlong")) {
        return CDT_PARSE_ERR_SIZE;
    }
    if (strstr(base, "threads_over")) {
        return CDT_PARSE_ERR_SIZE;
    }
    if (strstr(base, "state_enum") || strstr(base, "end_reason")) {
        return CDT_PARSE_ERR_UNKNOWN_ENUM;
    }
    return CDT_PARSE_ERR_FIELD; /* 其余非法用例：缺字段/越界/一致性等 */
}

static void run_fixture(const char *path)
{
    size_t len;
    unsigned char *buf = read_file(path, &len);
    cdt_state_store_t st;
    cdt_parse_result_t r;
    char name[512];
    char detail[128];
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;

    if (!buf) {
        snprintf(name, sizeof(name), "read %s", path);
        check(0, name, "无法读取文件");
        return;
    }
    cdt_state_store_init(&st);
    r = cdt_state_store_apply(&st, buf, len);
    if (strncmp(base, "valid_", 6) == 0) {
        snprintf(name, sizeof(name), "fixture %s → OK", base);
        snprintf(detail, sizeof(detail), "合法 fixture 被拒绝，实际码=%d", (int)r);
        check(r == CDT_PARSE_OK, name, detail);
    } else {
        cdt_parse_result_t want = expect_code_for(base);
        snprintf(name, sizeof(name), "fixture %s → %d", base, (int)want);
        snprintf(detail, sizeof(detail), "错误码不符，实际码=%d", (int)r);
        check(r == want, name, detail);
    }
    free(buf);
}

/* ---------- 内置语义用例 ---------- */

/* 最小合法包骨架；seq 用 %llu 填充。*/
static const char *TMPL =
    "{\"schema_version\":1,\"kind\":\"state\",\"bridge_epoch\":\"t-1\",\"seq\":%llu,"
    "\"generated_at_ms\":null,"
    "\"source\":{\"kind\":\"mock\",\"connected\":true,\"stale\":false,\"last_event_at_ms\":null},"
    "\"selected_thread_id\":null,\"threads_total\":0,\"threads_truncated\":false,"
    "\"threads\":[],\"usage\":{\"available\":false,\"updated_at_ms\":null,"
    "\"windows_total\":0,\"windows_truncated\":false,\"windows\":[]}}";

static void test_seq_semantics(void)
{
    cdt_state_store_t st;
    char buf[1024];
    char detail[128];
    cdt_parse_result_t r;

    cdt_state_store_init(&st);
    snprintf(buf, sizeof(buf), TMPL, 5ULL);
    r = cdt_state_store_apply(&st, buf, strlen(buf));
    snprintf(detail, sizeof(detail), "实际码=%d", (int)r);
    check(r == CDT_PARSE_OK, "seq=5 首次应用 OK", detail);

    snprintf(buf, sizeof(buf), TMPL, 5ULL);
    r = cdt_state_store_apply(&st, buf, strlen(buf));
    snprintf(detail, sizeof(detail), "实际码=%d", (int)r);
    check(r == CDT_PARSE_IGNORED_STALE_SEQ, "seq=5 重复 → IGNORED_STALE_SEQ", detail);

    snprintf(buf, sizeof(buf), TMPL, 4ULL);
    r = cdt_state_store_apply(&st, buf, strlen(buf));
    snprintf(detail, sizeof(detail), "实际码=%d", (int)r);
    check(r == CDT_PARSE_IGNORED_STALE_SEQ, "seq=4 回退 → IGNORED_STALE_SEQ", detail);

    snprintf(buf, sizeof(buf), TMPL, 6ULL);
    r = cdt_state_store_apply(&st, buf, strlen(buf));
    check(r == CDT_PARSE_OK, "seq=6 递增 → OK", NULL);
}

static void test_keep_last_valid(void)
{
    cdt_state_store_t st;
    char good[1024], bad[1024];
    const cdt_app_state_t *snap;
    cdt_parse_result_t r;

    cdt_state_store_init(&st);
    snprintf(good, sizeof(good), TMPL, 1ULL);
    check(cdt_state_store_apply(&st, good, strlen(good)) == CDT_PARSE_OK,
          "好包先应用", NULL);

    /* 坏 JSON（截断） */
    memcpy(bad, good, strlen(good));
    bad[strlen(good) / 2] = '\0';
    r = cdt_state_store_apply(&st, bad, strlen(good) / 2);
    check(r != CDT_PARSE_OK && r != CDT_PARSE_IGNORED_STALE_SEQ,
          "截断 JSON → ERR", NULL);
    snap = cdt_state_store_snapshot(&st);
    check(snap != NULL && snap->seq == 1, "拒绝后保留最后合法状态（seq=1）", NULL);

    /* 未知枚举 */
    snprintf(bad, sizeof(bad),
             "{\"schema_version\":1,\"kind\":\"state\",\"bridge_epoch\":\"t-1\",\"seq\":2,"
             "\"generated_at_ms\":null,"
             "\"source\":{\"kind\":\"mock\",\"connected\":true,\"stale\":false,\"last_event_at_ms\":null},"
             "\"selected_thread_id\":null,\"threads_total\":1,\"threads_truncated\":false,"
             "\"threads\":[{\"id\":\"a\",\"turn_id\":null,\"project\":\"p\",\"state\":\"sleeping\","
             "\"activity\":\"x\",\"updated_at_ms\":null,\"elapsed_ms\":0,\"waiting_ms\":0,"
             "\"end_reason\":null,\"attention\":null,"
             "\"plan\":{\"total\":0,\"truncated\":false,\"steps\":[]},"
             "\"context\":{\"used_tokens\":null,\"capacity_tokens\":null,\"used_percent\":null}}],"
             "\"usage\":{\"available\":false,\"updated_at_ms\":null,\"windows_total\":0,"
             "\"windows_truncated\":false,\"windows\":[]}}");
    r = cdt_state_store_apply(&st, bad, strlen(bad));
    check(r == CDT_PARSE_ERR_UNKNOWN_ENUM, "未知 state 枚举 → ERR_UNKNOWN_ENUM", NULL);
    snap = cdt_state_store_snapshot(&st);
    check(snap != NULL && snap->seq == 1 && snap->thread_count == 0,
          "整包拒绝不半应用（threads 仍为 0）", NULL);
}

static void test_epoch_reset(void)
{
    cdt_state_store_t st;
    char buf[1024];
    char *p;
    cdt_state_store_init(&st);
    snprintf(buf, sizeof(buf), TMPL, 9ULL);
    check(cdt_state_store_apply(&st, buf, strlen(buf)) == CDT_PARSE_OK, "epoch A seq9", NULL);
    p = strstr(buf, "t-1");
    p[1] = '2'; /* epoch t-2 */
    check(cdt_state_store_apply(&st, buf, strlen(buf)) == CDT_PARSE_OK,
          "新 epoch 低 seq 接受（epoch 重置语义）", NULL);
}

static void test_size_guard(void)
{
    cdt_state_store_t st;
    cdt_state_store_init(&st);
    /* >16384 字节：填充合法未知字段名（值 null）顶到超限 */
    {
        static char big[17000];
        size_t n = 0;
        int r;
        n = (size_t)snprintf(big, sizeof(big),
                             "{\"schema_version\":1,\"kind\":\"state\",\"bridge_epoch\":\"t\",\"seq\":1,"
                             "\"generated_at_ms\":null,"
                             "\"source\":{\"kind\":\"mock\",\"connected\":true,\"stale\":false,\"last_event_at_ms\":null},"
                             "\"selected_thread_id\":null,\"threads_total\":0,\"threads_truncated\":false,"
                             "\"threads\":[],\"usage\":{\"available\":false,\"updated_at_ms\":null,"
                             "\"windows_total\":0,\"windows_truncated\":false,\"windows\":[]},");
        while (n < sizeof(big) - 40) {
            n += (size_t)snprintf(big + n, 30, "\"x%zu\":null,", n);
        }
        n += (size_t)snprintf(big + n, 10, "\"z\":0}");
        r = cdt_state_store_apply(&st, big, n);
        check(r == CDT_PARSE_ERR_SIZE, "整包 >16384B → ERR_SIZE", NULL);
        (void)r;
    }
}

static void test_frame_bits(void)
{
    cdt_frame_t f;
    cdt_frame_clear(&f, 0);
    check(f.px[0] == 0x00 && f.px[CDT_FRAME_BYTES - 1] == 0x00, "clear 白", NULL);

    cdt_frame_set(&f, 0, 0, 1);
    check(f.px[0] == 0x80, "set(0,0)=黑 → 行0字节0 bit7（MSB=左）", NULL);
    cdt_frame_set(&f, 7, 0, 1);
    check(f.px[0] == 0x81, "set(7,0)=黑 → 行0字节0 bit0", NULL);
    cdt_frame_set(&f, 8, 0, 1);
    check(f.px[1] == 0x80, "set(8,0)=黑 → 行0字节1 bit7（跨字节）", NULL);
    cdt_frame_set(&f, 0, 1, 1);
    check(f.px[CDT_FRAME_STRIDE] == 0x80, "set(0,1)=黑 → 行1字节0 bit7（行优先）", NULL);
    cdt_frame_set(&f, 399, 299, 1);
    check(f.px[CDT_FRAME_BYTES - 1] == 0x01, "set(399,299)=黑 → 末字节 bit0", NULL);
    check(cdt_frame_get(&f, 399, 299) == 1 && cdt_frame_get(&f, 0, 0) == 1 &&
              cdt_frame_get(&f, 1, 0) == 0,
          "get 回读与 MSB 位序", NULL);
    cdt_frame_set(&f, 400, 0, 1);
    cdt_frame_set(&f, -1, 0, 1);
    cdt_frame_set(&f, 0, 300, 1);
    check(cdt_frame_get(&f, 400, 0) == -1, "越界坐标忽略", NULL);
}

static void test_zcode_kind(void)
{
    /* v1.1 增补 + 死枚举修复回归：四种合法 source.kind 全部须被接受并映射
     * （codex_desktop_observed 曾因长度写 21≠22 从未匹配成功）。 */
    static const char *KIND_TMPL =
        "{\"schema_version\":1,\"kind\":\"state\",\"bridge_epoch\":\"t-1\",\"seq\":%llu,"
        "\"generated_at_ms\":null,"
        "\"source\":{\"kind\":\"%s\",\"connected\":true,\"stale\":false,"
        "\"last_event_at_ms\":null},"
        "\"selected_thread_id\":null,\"threads_total\":0,\"threads_truncated\":false,"
        "\"threads\":[],\"usage\":{\"available\":false,\"updated_at_ms\":null,"
        "\"windows_total\":0,\"windows_truncated\":false,\"windows\":[]}}";
    static const struct {
        const char *str;
        cdt_source_kind_t want;
    } cases[] = {
        {"mock", CDT_SOURCE_MOCK},
        {"codex_bridge_owned", CDT_SOURCE_CODEX_BRIDGE_OWNED},
        {"codex_desktop_observed", CDT_SOURCE_CODEX_DESKTOP_OBSERVED},
        {"zcode_observed", CDT_SOURCE_ZCODE_OBSERVED},
    };
    cdt_state_store_t st;
    char buf[1024];
    char detail[160];
    size_t i;
    cdt_parse_result_t r;
    const cdt_app_state_t *snap;

    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        cdt_state_store_init(&st);
        snprintf(buf, sizeof(buf), KIND_TMPL, (unsigned long long)(i + 1),
                 cases[i].str);
        r = cdt_state_store_apply(&st, buf, strlen(buf));
        snprintf(detail, sizeof(detail), "kind=%s 实际码=%d", cases[i].str,
                 (int)r);
        check(r == CDT_PARSE_OK, "合法 source.kind 全接受", detail);
        snap = cdt_state_store_snapshot(&st);
        snprintf(detail, sizeof(detail), "kind=%s 映射不符", cases[i].str);
        check(snap != NULL && snap->source.kind == cases[i].want, "kind 映射正确",
              detail);
    }
}

int main(int argc, char **argv)
{
    int i;
    test_seq_semantics();
    test_zcode_kind();
    test_keep_last_valid();
    test_epoch_reset();
    test_size_guard();
    test_frame_bits();
    for (i = 1; i < argc; i++) {
        run_fixture(argv[i]);
    }
    printf("\n汇总: %d PASS, %d FAIL\n", passes, failures);
    return failures == 0 ? 0 : 1;
}
