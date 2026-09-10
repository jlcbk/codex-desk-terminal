/* cdt_json.c — 有界 JSON 词法原语实现（P1.4，A0）。契约见 cdt_json.h。*/
#include "cdt_json.h"

#include <stdlib.h>
#include <string.h>

int cdtj_peek(cdt_json_t *j)
{
    while (j->p < j->end) {
        uint8_t c = *j->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            j->p++;
            continue;
        }
        return c;
    }
    return -1;
}

cdtj_err_t cdtj_expect(cdt_json_t *j, char c)
{
    if (cdtj_peek(j) != (uint8_t)c) {
        return CDT_PARSE_ERR_FIELD;
    }
    j->p++;
    return CDT_PARSE_OK;
}

/* ---- UTF-8 ---- */

/* 校验并越过 p 处一个 UTF-8 码点；返回序列字节数，非法返回 0。*/
static int utf8_step(const uint8_t *p, const uint8_t *end)
{
    uint8_t c = p[0];
    int n;
    int i;
    if (c < 0x80) {
        return 1; /* ASCII（JSON 字符串原始字节中不应出现控制字符，由调用方语境处理）*/
    } else if ((c & 0xE0) == 0xC0) {
        n = 2;
        if (n == 2 && c < 0xC2) {
            return 0; /* 过长编码 */
        }
    } else if ((c & 0xF0) == 0xE0) {
        n = 3;
    } else if ((c & 0xF8) == 0xF0) {
        n = 4;
    } else {
        return 0; /* 0x80..0xBF 孤立续字节 / 0xF8+ */
    }
    if ((size_t)(end - p) < (size_t)n) {
        return 0; /* 截断序列 */
    }
    for (i = 1; i < n; i++) {
        if ((p[i] & 0xC0) != 0x80) {
            return 0;
        }
    }
    if (n == 3) {
        uint32_t cp = ((uint32_t)(c & 0x0F) << 12) | ((uint32_t)(p[1] & 0x3F) << 6) | (p[2] & 0x3F);
        if (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF)) {
            return 0; /* 过长 / 代理区 */
        }
    } else if (n == 4) {
        uint32_t cp = ((uint32_t)(c & 0x07) << 18) | ((uint32_t)(p[1] & 0x3F) << 12) |
                      ((uint32_t)(p[2] & 0x3F) << 6) | (p[3] & 0x3F);
        if (cp < 0x10000 || cp > 0x10FFFF) {
            return 0;
        }
    }
    return n;
}

static cdtj_err_t emit_byte(char *out, size_t cap, size_t *len, uint8_t b)
{
    if (out != NULL) {
        if (*len + 1 > cap - 1) { /* 需留 NUL 位；越限拒绝不截断 */
            return CDT_PARSE_ERR_SIZE;
        }
        out[*len] = (char)b;
    } else if (*len + 1 > CDT_STATE_JSON_MAX_BYTES) {
        return CDT_PARSE_ERR_SIZE; /* 无缓冲模式仅需防御性上限 */
    }
    (*len)++;
    return CDT_PARSE_OK;
}

static cdtj_err_t emit_cp(char *out, size_t cap, size_t *len, uint32_t cp)
{
    uint8_t buf[4];
    int n, i;
    cdtj_err_t e;
    if (cp < 0x80) {
        return emit_byte(out, cap, len, (uint8_t)cp);
    } else if (cp < 0x800) {
        n = 2;
        buf[0] = (uint8_t)(0xC0 | (cp >> 6));
        buf[1] = (uint8_t)(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        n = 3;
        buf[0] = (uint8_t)(0xE0 | (cp >> 12));
        buf[1] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        buf[2] = (uint8_t)(0x80 | (cp & 0x3F));
    } else {
        n = 4;
        buf[0] = (uint8_t)(0xF0 | (cp >> 18));
        buf[1] = (uint8_t)(0x80 | ((cp >> 12) & 0x3F));
        buf[2] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        buf[3] = (uint8_t)(0x80 | (cp & 0x3F));
    }
    for (i = 0; i < n; i++) {
        e = emit_byte(out, cap, len, buf[i]);
        if (e != CDT_PARSE_OK) {
            return e;
        }
    }
    return CDT_PARSE_OK;
}

static cdtj_err_t read_hex4(cdt_json_t *j, uint32_t *out)
{
    uint32_t v = 0;
    int i;
    if (j->end - j->p < 4) {
        return CDT_PARSE_ERR_TRUNCATED_CP;
    }
    for (i = 0; i < 4; i++) {
        char h = (char)j->p[i];
        v <<= 4;
        if (h >= '0' && h <= '9') {
            v |= (uint32_t)(h - '0');
        } else if (h >= 'a' && h <= 'f') {
            v |= (uint32_t)(h - 'a' + 10);
        } else if (h >= 'A' && h <= 'F') {
            v |= (uint32_t)(h - 'A' + 10);
        } else {
            return CDT_PARSE_ERR_FIELD;
        }
    }
    j->p += 4;
    *out = v;
    return CDT_PARSE_OK;
}

cdtj_err_t cdtj_read_string(cdt_json_t *j, char *out, size_t cap, size_t *out_len)
{
    size_t len = 0;
    cdtj_err_t e;
    if (out != NULL && cap == 0) {
        return CDT_PARSE_ERR_SIZE;
    }
    e = cdtj_expect(j, '"');
    if (e != CDT_PARSE_OK) {
        return e;
    }
    while (j->p < j->end) {
        uint8_t c = *j->p;
        if (c == '"') {
            j->p++;
            if (out != NULL) {
                out[len] = '\0';
            }
            if (out_len != NULL) {
                *out_len = len;
            }
            return CDT_PARSE_OK;
        }
        if (c == '\\') {
            j->p++;
            if (j->p >= j->end) {
                return CDT_PARSE_ERR_FIELD;
            }
            c = *j->p;
            j->p++;
            switch (c) {
            case '"': e = emit_byte(out, cap, &len, '"'); break;
            case '\\': e = emit_byte(out, cap, &len, '\\'); break;
            case '/': e = emit_byte(out, cap, &len, '/'); break;
            case 'b': e = emit_byte(out, cap, &len, '\b'); break;
            case 'f': e = emit_byte(out, cap, &len, '\f'); break;
            case 'n': e = emit_byte(out, cap, &len, '\n'); break;
            case 'r': e = emit_byte(out, cap, &len, '\r'); break;
            case 't': e = emit_byte(out, cap, &len, '\t'); break;
            case 'u': {
                uint32_t cp;
                e = read_hex4(j, &cp);
                if (e != CDT_PARSE_OK) {
                    return e;
                }
                if (cp >= 0xD800 && cp <= 0xDBFF) { /* 高代理必须跟低代理 */
                    uint32_t lo;
                    if (j->end - j->p >= 6 && j->p[0] == '\\' && j->p[1] == 'u') {
                        j->p += 2;
                        e = read_hex4(j, &lo);
                        if (e != CDT_PARSE_OK) {
                            return e;
                        }
                    } else {
                        return CDT_PARSE_ERR_TRUNCATED_CP;
                    }
                    if (lo < 0xDC00 || lo > 0xDFFF) {
                        return CDT_PARSE_ERR_TRUNCATED_CP;
                    }
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    return CDT_PARSE_ERR_TRUNCATED_CP; /* 孤立低代理 */
                }
                e = emit_cp(out, cap, &len, cp);
                break;
            }
            default:
                return CDT_PARSE_ERR_FIELD;
            }
            if (e != CDT_PARSE_OK) {
                return e;
            }
            continue;
        }
        if (c < 0x20) {
            return CDT_PARSE_ERR_FIELD; /* 原始控制字符非法 */
        }
        if (c < 0x80) {
            j->p++;
            e = emit_byte(out, cap, &len, c);
            if (e != CDT_PARSE_OK) {
                return e;
            }
            continue;
        }
        { /* 多字节 UTF-8 原始序列 */
            int n = utf8_step(j->p, j->end);
            int i;
            if (n == 0) {
                return CDT_PARSE_ERR_TRUNCATED_CP;
            }
            for (i = 0; i < n; i++) {
                e = emit_byte(out, cap, &len, j->p[i]);
                if (e != CDT_PARSE_OK) {
                    return e;
                }
            }
            j->p += (size_t)n;
        }
    }
    return CDT_PARSE_ERR_FIELD; /* 未闭合 */
}

cdtj_err_t cdtj_read_number(cdt_json_t *j, bool *is_integer, int64_t *ival, double *dval)
{
    const uint8_t *start;
    bool neg = false;
    bool isint = true;
    uint64_t mag = 0;
    bool overflow = false;
    if (cdtj_peek(j) == '-') {
        neg = true;
        j->p++;
    }
    if (j->p >= j->end) {
        return CDT_PARSE_ERR_FIELD;
    }
    start = j->p;
    if (*j->p == '0') {
        j->p++;
        if (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
            return CDT_PARSE_ERR_FIELD; /* 前导零 */
        }
    } else if (*j->p >= '1' && *j->p <= '9') {
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
            if (mag > (UINT64_MAX - 9) / 10) {
                overflow = true; /* 继续消费完整字面量再判错 */
            }
            if (!overflow) {
                mag = mag * 10 + (uint64_t)(*j->p - '0');
                if (mag > (uint64_t)INT64_MAX + 1) {
                    overflow = true;
                }
            }
            j->p++;
        }
    } else {
        return CDT_PARSE_ERR_FIELD;
    }
    if (j->p < j->end && *j->p == '.') {
        isint = false;
        j->p++;
        if (!(j->p < j->end && *j->p >= '0' && *j->p <= '9')) {
            return CDT_PARSE_ERR_FIELD;
        }
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
            j->p++;
        }
    }
    if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
        isint = false;
        j->p++;
        if (j->p < j->end && (*j->p == '+' || *j->p == '-')) {
            j->p++;
        }
        if (!(j->p < j->end && *j->p >= '0' && *j->p <= '9')) {
            return CDT_PARSE_ERR_FIELD;
        }
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
            j->p++;
        }
    }
    (void)start;
    if (isint) {
        if (overflow) {
            return CDT_PARSE_ERR_FIELD; /* 整数字面量超 int64：字段最大 2^53-1，必非法 */
        }
        if (ival != NULL) { /* skip_value 路径传 NULL：仅消费字面量 */
            if (neg) {
                *ival = (mag == (uint64_t)INT64_MAX + 1) ? INT64_MIN : -(int64_t)mag;
            } else {
                *ival = (int64_t)mag;
            }
        }
    } else {
        /* 用简易 strtod：构造临时 NUL 结尾串（长度受 16KiB 预算约束）。*/
        char buf[64];
        size_t n = (size_t)(j->p - start) + (neg ? 1 : 0);
        const uint8_t *s = neg ? start - 1 : start;
        if (n >= sizeof(buf)) {
            return CDT_PARSE_ERR_FIELD; /* 异常长数字字面量（字段为百分比/毫秒）*/
        }
        memcpy(buf, s, n);
        buf[n] = '\0';
        *dval = strtod(buf, NULL);
    }
    if (is_integer != NULL) {
        *is_integer = isint;
    }
    return CDT_PARSE_OK;
}

cdtj_err_t cdtj_read_bool(cdt_json_t *j, bool *out)
{
    (void)cdtj_peek(j); /* 跳过前导空白（peek 会推进 p）*/
    if (j->end - j->p >= 4 && memcmp(j->p, "true", 4) == 0) {
        j->p += 4;
        *out = true;
        return CDT_PARSE_OK;
    }
    if (j->end - j->p >= 5 && memcmp(j->p, "false", 5) == 0) {
        j->p += 5;
        *out = false;
        return CDT_PARSE_OK;
    }
    return CDT_PARSE_ERR_FIELD;
}

cdtj_err_t cdtj_expect_null(cdt_json_t *j)
{
    (void)cdtj_peek(j); /* 跳过前导空白（peek 会推进 p）*/
    if (j->end - j->p >= 4 && memcmp(j->p, "null", 4) == 0) {
        j->p += 4;
        return CDT_PARSE_OK;
    }
    return CDT_PARSE_ERR_FIELD;
}

static cdtj_err_t skip_literal(cdt_json_t *j, const char *lit, size_t n)
{
    if ((size_t)(j->end - j->p) >= n && memcmp(j->p, lit, n) == 0) {
        j->p += n;
        return CDT_PARSE_OK;
    }
    return CDT_PARSE_ERR_FIELD;
}

cdtj_err_t cdtj_skip_value(cdt_json_t *j)
{
    int c = cdtj_peek(j);
    cdtj_err_t e;
    if (c < 0) {
        return CDT_PARSE_ERR_FIELD;
    }
    switch ((uint8_t)c) {
    case '{':
    case '[': {
        char open = (char)c;
        char close = (open == '{') ? '}' : ']';
        j->p++;
        j->depth++;
        if (j->depth > CDT_STATE_JSON_MAX_DEPTH) {
            return CDT_PARSE_ERR_DEPTH;
        }
        e = cdtj_expect(j, close); /* 空容器 */
        if (e == CDT_PARSE_OK) {
            j->depth--;
            return CDT_PARSE_OK;
        }
        for (;;) {
            if (open == '{') {
                e = cdtj_read_string(j, NULL, 0, NULL); /* 键 */
                if (e != CDT_PARSE_OK) {
                    return e;
                }
                e = cdtj_expect(j, ':');
                if (e != CDT_PARSE_OK) {
                    return e;
                }
            }
            e = cdtj_skip_value(j);
            if (e != CDT_PARSE_OK) {
                return e;
            }
            c = cdtj_peek(j);
            if (c == ',') {
                j->p++;
                continue;
            }
            return cdtj_expect(j, close) == CDT_PARSE_OK
                       ? (j->depth--, CDT_PARSE_OK)
                       : CDT_PARSE_ERR_FIELD;
        }
    }
    case '"':
        return cdtj_read_string(j, NULL, 0, NULL);
    case 't':
        return skip_literal(j, "true", 4);
    case 'f':
        return skip_literal(j, "false", 5);
    case 'n':
        return skip_literal(j, "null", 4);
    default:
        return cdtj_read_number(j, NULL, NULL, NULL);
    }
}

cdtj_err_t cdtj_expect_eof(cdt_json_t *j)
{
    return cdtj_peek(j) < 0 ? CDT_PARSE_OK : CDT_PARSE_ERR_FIELD;
}
