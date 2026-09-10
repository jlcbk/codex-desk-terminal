"""Redaction helpers for the Codex bridge（P3.1）。

复制自 scripts/probe/redact.py（P0.2；探针目录保持不动），并做两处加强
（仅本副本，探针版行为不变）：

1. 任意字符串值里出现的邮箱 / API key / Bearer token 一律替换为 "<redacted>"
   （探针版只按键名脱敏，字符串内部的凭证会漏）；
2. 新增 scrub_text()：单行摘要清洗（换行折叠 + 邮箱/凭证正则 + 全局 home 路径
   前缀改写 + UTF-8 码点安全截断），供 adapter 生成 attention/summary 用。

Policy（与 P0.2 一致）:
- API keys / tokens / cookies / env values / account ids -> "<redacted>"
- home 路径 -> "~" 形式（保留结构，去掉含用户名的前缀）
- 用户内容（preview / 消息文本 / 线程名）-> "<content len=N>"，正文永不落盘
- token 计数（inputTokens/totalTokens/modelContextWindow 等）是协议事实，保留
"""

import os
import re

REDACTED = "<redacted>"

_SECRET_KEY_RE = re.compile(
    r"(token|secret|cookie|authorization|auth_|api[_-]?key|password|credential|"
    r"sessionid|session_id|jwt|bearer|account_?id|accountid|email|chatgpt)",
    re.IGNORECASE,
)
_CONTENT_KEY_RE = re.compile(
    r"(preview|text|message|input|output|content|summary|title|name|instructions|"
    r"prompt|agentnickname|agentrole)",
    re.IGNORECASE,
)
# Token *counts* are protocol facts (context usage), not credentials.
_TOKEN_COUNT_KEY_RE = re.compile(
    r"(input_?tokens|output_?tokens|cached_?input_?tokens|total_?tokens|token_?usage|"
    r"context_?window|modelcontextwindow)",
    re.IGNORECASE,
)
# Keys whose (non-secret) values we keep because they carry protocol facts.
_KEEP_KEY_RE = re.compile(r"(method|id$|^id|type|status|state|count|total|"
                          r"used_percent|percent|window|limit|resets|duration|"
                          r"plan|credits|unlimited|version|platform|codexhome|"
                          r"cwd|threadid|turnid|itemid|ephemeral|flags|source|"
                          r"createdat|updatedat|recencyat|path$|^path|primary|secondary)",
                          re.IGNORECASE)

# 字符串内部凭证模式（本副本加强项）。长度阈值取真实凭证的下限
# （OpenAI ≥40、GitHub ≥36、Slack ≥24 字符），避免 "desk-terminal" 这类
# 普通词里的 "sk-" 触发误报。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SECRET_TEXT_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}"
    r"|api[_-]?key\s*[=:]\s*\S+|token\s*[=:]\s*\S+)",
    re.IGNORECASE,
)

CODEX_HOME = os.path.expanduser("~")


def scrub_path(value):
    """Replace the user's home prefix with ~ and drop other /Users/<name> prefixes."""
    if not isinstance(value, str):
        return value
    if value.startswith(CODEX_HOME + os.sep):
        return "~" + value[len(CODEX_HOME):]
    return re.sub(r"^/Users/[^/]+", "~", value)


def scrub_home_global(text):
    """scrub_path 的全局版：任意位置的 home 前缀都改写为 ~/（摘要用）。"""
    if not isinstance(text, str):
        return text
    escaped = re.escape(CODEX_HOME + os.sep)
    return re.sub(escaped + r"[^\s'\"]*", lambda m: "~" + m.group(0)[len(CODEX_HOME):], text)


def scrub_text(value, max_bytes=160):
    """单行摘要清洗：折叠空白、脱敏邮箱/凭证/home 路径、码点安全截断。

    adapter 产出的 summary 一律经过本函数；凭证/邮箱/home 路径不进入
    AppState、fixture 或日志（INTERFACES §8 / AGENTS.md 红线）。
    """
    if not isinstance(value, str):
        return value
    s = re.sub(r"\s+", " ", value).strip()
    s = _EMAIL_RE.sub(REDACTED, s)
    s = _SECRET_TEXT_RE.sub(REDACTED, s)
    s = scrub_home_global(s)
    raw = s.encode("utf-8")
    if len(raw) > max_bytes:
        s = raw[:max_bytes].decode("utf-8", errors="ignore")
    return s


def _scrub_value(value, depth=0, allow_content=False):
    if depth > 14:
        return REDACTED
    if isinstance(value, dict):
        return _scrub_obj(value, depth)
    if isinstance(value, list):
        return [_scrub_value(v, depth + 1, allow_content) for v in value[:400]]
    if isinstance(value, str):
        # 本副本加强：字符串值也过一遍邮箱/凭证正则（探针版只 scrub 路径）。
        return scrub_text(scrub_path(value), max_bytes=4096)
    return value


def _scrub_obj(obj, depth=0):
    out = {}
    for key, value in obj.items():
        if _TOKEN_COUNT_KEY_RE.search(key):
            out[key] = _scrub_value(value, depth + 1)
            continue
        if _SECRET_KEY_RE.search(key):
            out[key] = REDACTED
            continue
        if not _KEEP_KEY_RE.search(key) and _CONTENT_KEY_RE.search(key):
            # user content: keep only a length marker for strings
            if isinstance(value, str):
                out[key] = "<content len=%d>" % len(value)
            elif value is None or isinstance(value, (bool, int, float)):
                out[key] = value
            else:
                out[key] = REDACTED
            continue
        if isinstance(value, dict):
            out[key] = _scrub_obj(value, depth + 1)
        elif isinstance(value, list):
            out[key] = [_scrub_value(value_item, depth + 1) for value_item in value[:400]]
        else:
            out[key] = _scrub_value(value, depth + 1)
    return out


def scrub(obj):
    """Recursively redact a decoded JSON structure for storage as evidence."""
    if isinstance(obj, dict):
        return _scrub_obj(obj)
    if isinstance(obj, list):
        return [_scrub_value(v) for v in obj]
    return _scrub_value(obj)
