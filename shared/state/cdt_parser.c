/* cdt_parser.c — AppState schema 方向解析实现。契约见 cdt_parser.h。*/
#include "cdt_parser.h"

#include <string.h>

#include "cdt_json.h"

/* ---------- 小工具 ---------- */

#define KEY_MAX 32

typedef struct {
    char buf[KEY_MAX];
    size_t len;
    bool overlong; /* 键长超 KEY_MAX：必为未知键 */
} key_t;

static cdtj_err_t read_key(cdt_json_t *j, key_t *k)
{
    k->overlong = false;
    return cdtj_read_string(j, k->buf, KEY_MAX, &k->len); /* 越限 → ERR_SIZE：键超长视为非法字段名 */
}

static bool key_is(const key_t *k, const char *name)
{
    size_t n = strlen(name);
    return !k->overlong && k->len == n && memcmp(k->buf, name, n) == 0;
}

typedef struct {
    cdt_json_t j;
    cdt_parse_result_t err;
} parser_t;

static cdtj_err_t rd_str(cdt_json_t *j, char *out, size_t cap)
{
    return cdtj_read_string(j, out, cap, NULL);
}

/* 整数（可为 null）：*present=false 表示 JSON null。*/
static cdtj_err_t rd_int_null(cdt_json_t *j, bool *present, int64_t *out)
{
    if (cdtj_peek(j) == 'n') {
        *present = false;
        return cdtj_expect_null(j);
    }
    *present = true;
    {
        bool isint;
        int64_t v;
        cdtj_err_t e = cdtj_read_number(j, &isint, &v, NULL);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (!isint) {
            return CDT_PARSE_ERR_FIELD;
        }
        *out = v;
    }
    return CDT_PARSE_OK;
}

static cdtj_err_t rd_int(cdt_json_t *j, int64_t *out)
{
    bool present;
    cdtj_err_t e = rd_int_null(j, &present, out);
    if (e == CDT_PARSE_OK && !present) {
        return CDT_PARSE_ERR_FIELD; /* 不允许 null 的整数字段 */
    }
    return e;
}

/* 非负整数 → uint64（负值 ERR_FIELD）。*/
static cdtj_err_t rd_uint(cdt_json_t *j, uint64_t *out)
{
    int64_t v;
    cdtj_err_t e = rd_int(j, &v);
    if (e != CDT_PARSE_OK) {
        return e;
    }
    if (v < 0) {
        return CDT_PARSE_ERR_FIELD;
    }
    *out = (uint64_t)v;
    return CDT_PARSE_OK;
}

/* 0..100 数值（int/float），可为 null。*/
static cdtj_err_t rd_percent_null(cdt_json_t *j, bool *present, double *out)
{
    if (cdtj_peek(j) == 'n') {
        *present = false;
        return cdtj_expect_null(j);
    }
    *present = true;
    {
        bool isint;
        int64_t iv;
        double dv;
        cdtj_err_t e = cdtj_read_number(j, &isint, &iv, &dv);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        *out = isint ? (double)iv : dv;
        if (*out < 0.0 || *out > 100.0) {
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return CDT_PARSE_OK;
}

static cdtj_err_t rd_bool(cdt_json_t *j, bool *out)
{
    return cdtj_read_bool(j, out);
}

/* 枚举：按名匹配；未命中 → ERR_UNKNOWN_ENUM（§3 state 行）。*/
#define MATCH_ENUM(j, out, val, name)                     \
    do {                                                  \
        char tmp_[16];                                    \
        size_t n_;                                        \
        cdtj_err_t e_ = cdtj_read_string(j, tmp_, sizeof(tmp_), &n_); \
        if (e_ != CDT_PARSE_OK) {                         \
            return e_;                                    \
        }                                                 \
        if (n_ == sizeof(name) - 1 && memcmp(tmp_, name, n_) == 0) { \
            *(out) = (val);                               \
            return CDT_PARSE_OK;                          \
        }                                                 \
        return CDT_PARSE_ERR_UNKNOWN_ENUM;                \
    } while (0)

static cdtj_err_t parse_source(cdt_json_t *j, cdt_source_t *out)
{
    uint32_t seen = 0; /* bit: kind=1 connected=2 stale=4 last_event_at_ms=8 */
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD; /* 缺全部必填 */
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "kind")) {
            uint32_t bit = 1;
            if (seen & bit) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= bit;
            {
                char tmp[24];
                size_t n;
                e = cdtj_read_string(j, tmp, sizeof(tmp), &n);
                if (e != CDT_PARSE_OK) {
                    return e;
                }
                if (n == 4 && memcmp(tmp, "mock", 4) == 0) {
                    out->kind = CDT_SOURCE_MOCK;
                } else if (n == 18 && memcmp(tmp, "codex_bridge_owned", 18) == 0) {
                    out->kind = CDT_SOURCE_CODEX_BRIDGE_OWNED;
                } else if (n == 21 && memcmp(tmp, "codex_desktop_observed", 21) == 0) {
                    out->kind = CDT_SOURCE_CODEX_DESKTOP_OBSERVED;
                } else if (n == 14 && memcmp(tmp, "zcode_observed", 14) == 0) {
                    out->kind = CDT_SOURCE_ZCODE_OBSERVED; /* v1.1 增补 */
                } else {
                    return CDT_PARSE_ERR_UNKNOWN_ENUM;
                }
            }
        } else if (key_is(&k, "connected")) {
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = rd_bool(j, &out->connected);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        } else if (key_is(&k, "stale")) {
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = rd_bool(j, &out->stale);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        } else if (key_is(&k, "last_event_at_ms")) {
            if (seen & 8) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 8;
            e = rd_int_null(j, &out->last_event_at_ms_present, &out->last_event_at_ms);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        } else {
            e = cdtj_skip_value(j);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0xF ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t parse_attention(cdt_json_t *j, cdt_thread_t *th)
{
    /* 调用前已确认非 null（§3：attention 允许整包 null）*/
    uint32_t seen = 0; /* pending_count=1 summary=2 */
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    th->attention_present = true;
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "pending_count")) {
            uint64_t v;
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_uint(j, &v);
            if (e != CDT_PARSE_OK) {
                return e;
            }
            if (v > 65535) {
                return CDT_PARSE_ERR_FIELD; /* A0 冻结上限 */
            }
            th->attention.pending_count = (uint16_t)v;
        } else if (key_is(&k, "summary")) {
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = rd_str(j, th->attention.summary, CDT_MAX_ATTENTION_SUMMARY_BYTES + 1);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        } else {
            e = cdtj_skip_value(j);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0x3 ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t parse_plan(cdt_json_t *j, cdt_plan_t *out)
{
    uint32_t seen = 0; /* total=1 truncated=2 steps=4 */
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "total")) {
            uint64_t v;
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_uint(j, &v);
            if (e != CDT_PARSE_OK) {
                return e;
            }
            if (v > 65535) {
                return CDT_PARSE_ERR_FIELD;
            }
            out->total = (uint16_t)v;
        } else if (key_is(&k, "truncated")) {
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = rd_bool(j, &out->truncated);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        } else if (key_is(&k, "steps")) {
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = cdtj_expect(j, '[');
            if (e != CDT_PARSE_OK) {
                return e;
            }
            j->depth++;
            if (j->depth > CDT_STATE_JSON_MAX_DEPTH) {
                return CDT_PARSE_ERR_DEPTH;
            }
            out->step_count = 0;
            if (cdtj_peek(j) == ']') {
                j->p++;
                j->depth--;
            } else {
                for (;;) {
                    cdt_step_t *st;
                    if (out->step_count >= CDT_MAX_PLAN_STEPS) {
                        return CDT_PARSE_ERR_SIZE; /* §3 plan 行：最多 8 项 */
                    }
                    st = &out->steps[out->step_count];
                    {
                        uint32_t sseen = 0; /* text=1 status=2 */
                        e = cdtj_expect(j, '{');
                        if (e != CDT_PARSE_OK) {
                            return e;
                        }
                        for (;;) {
                            e = read_key(j, &k);
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            e = cdtj_expect(j, ':');
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            if (key_is(&k, "text")) {
                                if (sseen & 1) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                sseen |= 1;
                                e = rd_str(j, st->text, CDT_MAX_PLAN_TEXT_BYTES + 1);
                            } else if (key_is(&k, "status")) {
                                if (sseen & 2) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                sseen |= 2;
                                {
                                    char tmp[16];
                                    size_t n;
                                    e = cdtj_read_string(j, tmp, sizeof(tmp), &n);
                                    if (e != CDT_PARSE_OK) {
                                        return e;
                                    }
                                    if (n == 7 && memcmp(tmp, "pending", 7) == 0) {
                                        st->status = (uint8_t)CDT_STEP_STATUS_PENDING;
                                    } else if (n == 11 && memcmp(tmp, "in_progress", 11) == 0) {
                                        st->status = (uint8_t)CDT_STEP_STATUS_IN_PROGRESS;
                                    } else if (n == 9 && memcmp(tmp, "completed", 9) == 0) {
                                        st->status = (uint8_t)CDT_STEP_STATUS_COMPLETED;
                                    } else {
                                        return CDT_PARSE_ERR_UNKNOWN_ENUM;
                                    }
                                }
                            } else {
                                e = cdtj_skip_value(j);
                            }
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            {
                                int c = cdtj_peek(j);
                                if (c == ',') {
                                    j->p++;
                                    continue;
                                }
                                if (c == '}') {
                                    j->p++;
                                    break;
                                }
                                return CDT_PARSE_ERR_FIELD;
                            }
                        }
                        if (sseen != 0x3) {
                            return CDT_PARSE_ERR_FIELD;
                        }
                    }
                    out->step_count++;
                    {
                        int c = cdtj_peek(j);
                        if (c == ',') {
                            j->p++;
                            continue;
                        }
                        if (c == ']') {
                            j->p++;
                            j->depth--;
                            break;
                        }
                        return CDT_PARSE_ERR_FIELD;
                    }
                }
            }
        } else {
            e = cdtj_skip_value(j);
            if (e != CDT_PARSE_OK) {
                return e;
            }
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0x7 ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t parse_context(cdt_json_t *j, cdt_context_t *out)
{
    uint32_t seen = 0; /* used_tokens=1 capacity=4 percent=2 */
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "used_tokens")) {
            int64_t v;
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_int_null(j, &out->used_tokens_present, &v);
            if (e == CDT_PARSE_OK && out->used_tokens_present && v < 0) {
                return CDT_PARSE_ERR_FIELD;
            }
            if (e == CDT_PARSE_OK) {
                out->used_tokens = v;
            }
        } else if (key_is(&k, "capacity_tokens")) {
            int64_t v;
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = rd_int_null(j, &out->capacity_tokens_present, &v);
            if (e == CDT_PARSE_OK && out->capacity_tokens_present && v < 0) {
                return CDT_PARSE_ERR_FIELD;
            }
            if (e == CDT_PARSE_OK) {
                out->capacity_tokens = v;
            }
        } else if (key_is(&k, "used_percent")) {
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = rd_percent_null(j, &out->used_percent_present, &out->used_percent);
        } else {
            e = cdtj_skip_value(j);
        }
        if (e != CDT_PARSE_OK) {
            return e;
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0x7 ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t parse_thread(cdt_json_t *j, cdt_thread_t *out)
{
    /* bit: id=1 turn=2 project=4 state=8 activity=16 updated=32 elapsed=64
     *      waiting=128 end_reason=256 attention=512 plan=1024 context=2048 */
    uint32_t seen = 0;
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    memset(out, 0, sizeof(*out));
    out->state = CDT_THREAD_STATE_INVALID;
    out->end_reason = CDT_END_REASON_INVALID;
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "id")) {
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_str(j, out->id, CDT_MAX_ID_BYTES + 1);
        } else if (key_is(&k, "turn_id")) {
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            if (cdtj_peek(j) == 'n') {
                out->turn_id_present = false;
                e = cdtj_expect_null(j);
            } else {
                out->turn_id_present = true;
                e = rd_str(j, out->turn_id, CDT_MAX_TURN_ID_BYTES + 1);
            }
        } else if (key_is(&k, "project")) {
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = rd_str(j, out->project, CDT_MAX_PROJECT_BYTES + 1);
        } else if (key_is(&k, "state")) {
            if (seen & 8) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 8;
            {
                char tmp[16];
                size_t n;
                e = cdtj_read_string(j, tmp, sizeof(tmp), &n);
                if (e != CDT_PARSE_OK) {
                    return e;
                }
                if (n == 4 && memcmp(tmp, "idle", 4) == 0) {
                    out->state = CDT_THREAD_STATE_IDLE;
                } else if (n == 8 && memcmp(tmp, "thinking", 8) == 0) {
                    out->state = CDT_THREAD_STATE_THINKING;
                } else if (n == 7 && memcmp(tmp, "working", 7) == 0) {
                    out->state = CDT_THREAD_STATE_WORKING;
                } else if (n == 9 && memcmp(tmp, "needs_you", 9) == 0) {
                    out->state = CDT_THREAD_STATE_NEEDS_YOU;
                } else if (n == 4 && memcmp(tmp, "done", 4) == 0) {
                    out->state = CDT_THREAD_STATE_DONE;
                } else if (n == 5 && memcmp(tmp, "error", 5) == 0) {
                    out->state = CDT_THREAD_STATE_ERROR;
                } else {
                    return CDT_PARSE_ERR_UNKNOWN_ENUM;
                }
            }
        } else if (key_is(&k, "activity")) {
            if (seen & 16) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 16;
            e = rd_str(j, out->activity, CDT_MAX_ACTIVITY_BYTES + 1);
        } else if (key_is(&k, "updated_at_ms")) {
            if (seen & 32) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 32;
            e = rd_int_null(j, &out->updated_at_ms_present, &out->updated_at_ms);
        } else if (key_is(&k, "elapsed_ms")) {
            if (seen & 64) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 64;
            e = rd_uint(j, &out->elapsed_ms);
        } else if (key_is(&k, "waiting_ms")) {
            if (seen & 128) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 128;
            e = rd_uint(j, &out->waiting_ms);
        } else if (key_is(&k, "end_reason")) {
            if (seen & 256) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 256;
            if (cdtj_peek(j) == 'n') {
                e = cdtj_expect_null(j);
                if (e == CDT_PARSE_OK) {
                    out->end_reason = CDT_END_REASON_NULL;
                }
            } else {
                char tmp[16];
                size_t n;
                e = cdtj_read_string(j, tmp, sizeof(tmp), &n);
                if (e == CDT_PARSE_OK) {
                    if (n == 9 && memcmp(tmp, "completed", 9) == 0) {
                        out->end_reason = CDT_END_REASON_COMPLETED;
                    } else if (n == 6 && memcmp(tmp, "failed", 6) == 0) {
                        out->end_reason = CDT_END_REASON_FAILED;
                    } else if (n == 9 && memcmp(tmp, "cancelled", 9) == 0) {
                        out->end_reason = CDT_END_REASON_CANCELLED;
                    } else {
                        return CDT_PARSE_ERR_UNKNOWN_ENUM;
                    }
                }
            }
        } else if (key_is(&k, "attention")) {
            if (seen & 512) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 512;
            if (cdtj_peek(j) == 'n') {
                e = cdtj_expect_null(j);
                if (e == CDT_PARSE_OK) {
                    out->attention_present = false;
                }
            } else {
                e = parse_attention(j, out);
            }
        } else if (key_is(&k, "plan")) {
            if (seen & 1024) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1024;
            e = parse_plan(j, &out->plan);
        } else if (key_is(&k, "context")) {
            if (seen & 2048) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2048;
            e = parse_context(j, &out->context);
        } else {
            e = cdtj_skip_value(j);
        }
        if (e != CDT_PARSE_OK) {
            return e;
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0xFFF ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t parse_usage(cdt_json_t *j, cdt_usage_t *out)
{
    /* available=1 updated=32 total=2 truncated=4 windows=8 */
    uint32_t seen = 0;
    key_t k;
    cdtj_err_t e = cdtj_expect(j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    if (cdtj_peek(j) == '}') {
        j->p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "available")) {
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_bool(j, &out->available);
        } else if (key_is(&k, "updated_at_ms")) {
            if (seen & 32) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 32;
            e = rd_int_null(j, &out->updated_at_ms_present, &out->updated_at_ms);
        } else if (key_is(&k, "windows_total")) {
            uint64_t v;
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = rd_uint(j, &v);
            if (e == CDT_PARSE_OK) {
                if (v > 65535) {
                    return CDT_PARSE_ERR_FIELD;
                }
                out->windows_total = (uint16_t)v;
            }
        } else if (key_is(&k, "windows_truncated")) {
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = rd_bool(j, &out->windows_truncated);
        } else if (key_is(&k, "windows")) {
            if (seen & 8) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 8;
            e = cdtj_expect(j, '[');
            if (e != CDT_PARSE_OK) {
                return e;
            }
            j->depth++;
            if (j->depth > CDT_STATE_JSON_MAX_DEPTH) {
                return CDT_PARSE_ERR_DEPTH;
            }
            out->window_count = 0;
            if (cdtj_peek(j) == ']') {
                j->p++;
                j->depth--;
            } else {
                for (;;) {
                    cdt_usage_window_t *w;
                    if (out->window_count >= CDT_MAX_USAGE_WINDOWS) {
                        return CDT_PARSE_ERR_SIZE; /* §3 usage 行：最多 4 项 */
                    }
                    w = &out->windows[out->window_count];
                    {
                        uint32_t wseen = 0; /* id=1 label=2 percent=4 duration=8 resets=16 */
                        e = cdtj_expect(j, '{');
                        if (e != CDT_PARSE_OK) {
                            return e;
                        }
                        for (;;) {
                            e = read_key(j, &k);
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            e = cdtj_expect(j, ':');
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            if (key_is(&k, "id")) {
                                if (wseen & 1) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                wseen |= 1;
                                e = rd_str(j, w->id, CDT_MAX_ID_BYTES + 1);
                            } else if (key_is(&k, "label")) {
                                if (wseen & 2) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                wseen |= 2;
                                e = rd_str(j, w->label, CDT_MAX_USAGE_LABEL_BYTES + 1);
                            } else if (key_is(&k, "used_percent")) {
                                if (wseen & 4) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                wseen |= 4;
                                e = rd_percent_null(j, &w->used_percent_present, &w->used_percent);
                            } else if (key_is(&k, "duration_mins")) {
                                uint64_t v;
                                if (wseen & 8) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                wseen |= 8;
                                e = rd_uint(j, &v);
                                if (e == CDT_PARSE_OK) {
                                    if (v < 1 || v > 65535) {
                                        return CDT_PARSE_ERR_FIELD; /* 正整数分钟，上限 65535（A0 冻结）*/
                                    }
                                    w->duration_mins = (uint16_t)v;
                                }
                            } else if (key_is(&k, "resets_at_ms")) {
                                if (wseen & 16) {
                                    return CDT_PARSE_ERR_FIELD;
                                }
                                wseen |= 16;
                                e = rd_int_null(j, &w->resets_at_ms_present, &w->resets_at_ms);
                            } else {
                                e = cdtj_skip_value(j);
                            }
                            if (e != CDT_PARSE_OK) {
                                return e;
                            }
                            {
                                int c = cdtj_peek(j);
                                if (c == ',') {
                                    j->p++;
                                    continue;
                                }
                                if (c == '}') {
                                    j->p++;
                                    break;
                                }
                                return CDT_PARSE_ERR_FIELD;
                            }
                        }
                        if (wseen != 0x1F) {
                            return CDT_PARSE_ERR_FIELD;
                        }
                    }
                    out->window_count++;
                    {
                        int c = cdtj_peek(j);
                        if (c == ',') {
                            j->p++;
                            continue;
                        }
                        if (c == ']') {
                            j->p++;
                            j->depth--;
                            break;
                        }
                        return CDT_PARSE_ERR_FIELD;
                    }
                }
            }
        } else {
            e = cdtj_skip_value(j);
        }
        if (e != CDT_PARSE_OK) {
            return e;
        }
        {
            int c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            if (c == '}') {
                j->p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    return seen == 0x2F ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}

cdt_parse_result_t cdt_state_parse(const void *bytes, size_t len, cdt_app_state_t *out)
{
    cdt_json_t j;
    /* bit: ver=1 kind=2 epoch=4 seq=8 gen=16 source=32 selid=64
     *      ttotal=128 ttrunc=256 threads=512 usage=1024 */
    uint32_t seen = 0;
    key_t k;
    cdtj_err_t e;

    if (len == 0) {
        return CDT_PARSE_ERR_FIELD;
    }
    if (len > CDT_STATE_JSON_MAX_BYTES) {
        return CDT_PARSE_ERR_SIZE;
    }
    memset(out, 0, sizeof(*out));
    cdtj_init(&j, bytes, len);

    e = cdtj_expect(&j, '{');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    j.depth = 1;
    if (cdtj_peek(&j) == '}') {
        j.p++;
        return CDT_PARSE_ERR_FIELD;
    }
    for (;;) {
        e = read_key(&j, &k);
        if (e != CDT_PARSE_OK) {
            return e;
        }
        e = cdtj_expect(&j, ':');
        if (e != CDT_PARSE_OK) {
            return e;
        }
        if (key_is(&k, "schema_version")) {
            int64_t v;
            if (seen & 1) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1;
            e = rd_int(&j, &v);
            if (e == CDT_PARSE_OK && v != 1) {
                return CDT_PARSE_ERR_VERSION; /* §3：未知主版本拒绝 */
            }
        } else if (key_is(&k, "kind")) {
            char tmp[8];
            size_t n;
            if (seen & 2) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 2;
            e = cdtj_read_string(&j, tmp, sizeof(tmp), &n);
            if (e == CDT_PARSE_OK && !(n == 5 && memcmp(tmp, "state", 5) == 0)) {
                return CDT_PARSE_ERR_FIELD;
            }
        } else if (key_is(&k, "bridge_epoch")) {
            if (seen & 4) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 4;
            e = cdtj_read_string(&j, out->bridge_epoch, CDT_MAX_BRIDGE_EPOCH_BYTES + 1, NULL);
            /* 1–64 字节：空串即 len 0——read_string 不区分，补查 */
            if (e == CDT_PARSE_OK && out->bridge_epoch[0] == '\0') {
                return CDT_PARSE_ERR_FIELD;
            }
        } else if (key_is(&k, "seq")) {
            uint64_t v;
            if (seen & 8) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 8;
            e = rd_uint(&j, &v);
            if (e == CDT_PARSE_OK) {
                if (v > CDT_SEQ_MAX) {
                    return CDT_PARSE_ERR_FIELD; /* 0..2^53-1 */
                }
                out->seq = v;
            }
        } else if (key_is(&k, "generated_at_ms")) {
            if (seen & 16) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 16;
            e = rd_int_null(&j, &out->generated_at_ms_present, &out->generated_at_ms);
        } else if (key_is(&k, "source")) {
            if (seen & 32) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 32;
            e = parse_source(&j, &out->source);
        } else if (key_is(&k, "selected_thread_id")) {
            if (seen & 64) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 64;
            if (cdtj_peek(&j) == 'n') {
                out->selected_thread_id_present = false;
                e = cdtj_expect_null(&j);
            } else {
                out->selected_thread_id_present = true;
                e = rd_str(&j, out->selected_thread_id, CDT_MAX_ID_BYTES + 1);
            }
        } else if (key_is(&k, "threads_total")) {
            uint64_t v;
            if (seen & 128) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 128;
            e = rd_uint(&j, &v);
            if (e == CDT_PARSE_OK) {
                if (v > 65535) {
                    return CDT_PARSE_ERR_FIELD;
                }
                out->threads_total = (uint16_t)v;
            }
        } else if (key_is(&k, "threads_truncated")) {
            if (seen & 256) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 256;
            e = rd_bool(&j, &out->threads_truncated);
        } else if (key_is(&k, "threads")) {
            if (seen & 512) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 512;
            e = cdtj_expect(&j, '[');
            if (e != CDT_PARSE_OK) {
                return e;
            }
            j.depth++;
            if (j.depth > CDT_STATE_JSON_MAX_DEPTH) {
                return CDT_PARSE_ERR_DEPTH;
            }
            out->thread_count = 0;
            if (cdtj_peek(&j) == ']') {
                j.p++;
                j.depth--;
            } else {
                for (;;) {
                    if (out->thread_count >= CDT_MAX_THREADS) {
                        return CDT_PARSE_ERR_SIZE; /* §3：最多 8 项 */
                    }
                    e = parse_thread(&j, &out->threads[out->thread_count]);
                    if (e != CDT_PARSE_OK) {
                        return e;
                    }
                    out->thread_count++;
                    {
                        int c = cdtj_peek(&j);
                        if (c == ',') {
                            j.p++;
                            continue;
                        }
                        if (c == ']') {
                            j.p++;
                            j.depth--;
                            break;
                        }
                        return CDT_PARSE_ERR_FIELD;
                    }
                }
            }
        } else if (key_is(&k, "usage")) {
            if (seen & 1024) {
                return CDT_PARSE_ERR_FIELD;
            }
            seen |= 1024;
            e = parse_usage(&j, &out->usage);
        } else {
            e = cdtj_skip_value(&j);
        }
        if (e != CDT_PARSE_OK) {
            return e;
        }
        {
            int c = cdtj_peek(&j);
            if (c == ',') {
                j.p++;
                continue;
            }
            if (c == '}') {
                j.p++;
                break;
            }
            return CDT_PARSE_ERR_FIELD;
        }
    }
    if (seen != 0x7FF) {
        return CDT_PARSE_ERR_FIELD; /* 缺必填字段（§3 末段）*/
    }
    if (out->threads_total < out->thread_count) {
        return CDT_PARSE_ERR_FIELD;
    }
    if (out->usage.windows_total < out->usage.window_count) {
        return CDT_PARSE_ERR_FIELD;
    }
    { /* thread id 唯一 + selected_thread_id 一致性（§3）*/
        uint8_t i, t;
        for (i = 0; i < out->thread_count; i++) {
            for (t = (uint8_t)(i + 1); t < out->thread_count; t++) {
                if (strcmp(out->threads[i].id, out->threads[t].id) == 0) {
                    return CDT_PARSE_ERR_FIELD;
                }
            }
        }
        if (out->selected_thread_id_present) {
            bool found = false;
            for (i = 0; i < out->thread_count; i++) {
                if (strcmp(out->selected_thread_id, out->threads[i].id) == 0) {
                    found = true;
                    break;
                }
            }
            if (!found) {
                return CDT_PARSE_ERR_FIELD;
            }
        }
    }
    return cdtj_expect_eof(&j);
}
