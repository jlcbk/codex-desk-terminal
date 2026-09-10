/*
 * cdt_json.h — 有界 JSON 词法原语（P1.4，A0）
 *
 * 配套冻结契约 protocol/state.schema.json 与 shared/state/codex_state.h。
 * 设计约束（INTERFACES §3 完整消息行）：
 *   - 零动态分配：调用方提供一切缓冲。
 *   - 输入即冻结预算：整包 <=CDT_STATE_JSON_MAX_BYTES（调用方在进入本层前检查）。
 *   - 嵌套深度 <=CDT_STATE_JSON_MAX_DEPTH（根对象计 1），越界 ERR_DEPTH。
 *   - 字符串按 UTF-8 字节预算校验（cap），并验证 UTF-8 合法性（含 \u 代理对）。
 *   - 本层不了解 AppState 语义；schema 方向的解析见 cdt_parser.c。
 */
#ifndef CDT_JSON_H
#define CDT_JSON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "codex_state.h"

/* 错误码复用 cdt_parse_result_t 的 ERR_* 值（见 codex_state.h）。*/
typedef cdt_parse_result_t cdtj_err_t;

typedef struct {
    const uint8_t *p;
    const uint8_t *end;
    int depth; /* 当前已进入的容器层数（根为 1） */
} cdt_json_t;

static inline void cdtj_init(cdt_json_t *j, const void *bytes, size_t len)
{
    j->p = (const uint8_t *)bytes;
    j->end = j->p + len;
    j->depth = 0;
}

/* 跳过空白；返回当前字符（<0 表示输入结束）。*/
int cdtj_peek(cdt_json_t *j);

/* 期望并消费一个字面字符 '{' '}' '[' ']' ',' ':'；失败返回 ERR_FIELD。*/
cdtj_err_t cdtj_expect(cdt_json_t *j, char c);

/*
 * 解析一个 JSON 字符串（含两侧引号）。
 *   out==NULL：仅校验并跳过（用于未知键名/未知值）。
 *   out!=NULL：解码（处理 \\ \" \/ \b \f \n \r \t \uXXXX 含代理对）后写入 out，
 *              out_len 写出字节数（不含 NUL，NUL 由本函数补写）。
 *   cap 为 out 缓冲的字节容量（含 NUL 位置；解码后长度 > cap-1 → ERR_SIZE，
 *   绝不截断写入——对应 §3 字符串行与 codex_state.h 文件头约定）。
 *   原始字节或转义序列构成非法 UTF-8 → ERR_TRUNCATED_CP。
 */
cdtj_err_t cdtj_read_string(cdt_json_t *j, char *out, size_t cap, size_t *out_len);

/*
 * 解析数字。is_integer 区分整数字面量（无小数/指数部分）与浮点；
 * 整数超 int64 范围 → ERR_FIELD（我们的字段最大 2^53-1，越界即非法）。
 */
cdtj_err_t cdtj_read_number(cdt_json_t *j, bool *is_integer, int64_t *ival, double *dval);

cdtj_err_t cdtj_read_bool(cdt_json_t *j, bool *out);
cdtj_err_t cdtj_expect_null(cdt_json_t *j);

/*
 * 跳过任意一个 JSON 值（对象/数组递归受深度限制；字符串/数字/字面量校验语法）。
 * 用于 additionalProperties 允许的未知字段（值仍计入深度限制，§3 前向兼容规则）。
 */
cdtj_err_t cdtj_skip_value(cdt_json_t *j);

/* 顶层值结束后必须只剩空白；否则 ERR_FIELD。*/
cdtj_err_t cdtj_expect_eof(cdt_json_t *j);

#endif /* CDT_JSON_H */
