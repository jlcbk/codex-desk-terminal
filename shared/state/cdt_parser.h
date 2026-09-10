/*
 * cdt_parser.h — AppState JSON → cdt_app_state_t 有界解析（P1.4，A0）
 *
 * 规则真源：docs/INTERFACES.md §3（冻结）与 protocol/state.schema.json。
 * 语义：类型错误/越界/缺必填/未知枚举/非法 UTF-8/超深/超限 → 拒绝整包
 * （返回对应 ERR_*，不写入 out）；out 仅在返回 CDT_PARSE_OK 时有效。
 * 未知附加字段允许并跳过（值计入深度限制）；重复键视为 ERR_FIELD。
 * A0 冻结补充：duration_mins 上限 65535（与设备 uint16 对齐）。
 */
#ifndef CDT_PARSER_H
#define CDT_PARSER_H

#include <stddef.h>

#include "codex_state.h"

/* 解析完整 AppState JSON（bytes/len 为 UTF-8 字节，不含 NUL 要求）。*/
cdt_parse_result_t cdt_state_parse(const void *bytes, size_t len, cdt_app_state_t *out);

#endif /* CDT_PARSER_H */
