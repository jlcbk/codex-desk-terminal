#!/usr/bin/env python3
"""Redaction helpers for P0.2 probe evidence.

Policy (P0.2 safety rules):
- API keys / tokens / cookies / env values / account ids -> "<redacted>"
- Absolute home paths -> "~" form (keeps structure, drops username-bearing path prefix)
- Conversation previews / message bodies -> "<redacted>" (user content never stored)
- Unknown sensitive-looking keys are redacted conservatively.
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

CODEX_HOME = os.path.expanduser("~")


def scrub_path(value):
    """Replace the user's home prefix with ~ and drop other /Users/<name> prefixes."""
    if not isinstance(value, str):
        return value
    if value.startswith(CODEX_HOME + os.sep):
        return "~" + value[len(CODEX_HOME):]
    return re.sub(r"^/Users/[^/]+", "~", value)


def _scrub_value(value, depth=0, allow_content=False):
    if depth > 14:
        return REDACTED
    if isinstance(value, dict):
        return _scrub_obj(value, depth)
    if isinstance(value, list):
        return [_scrub_value(v, depth + 1, allow_content) for v in value[:400]]
    if isinstance(value, str):
        return scrub_path(value)
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
            out[key] = [_scrub_value(v, depth + 1) for v in value[:400]]
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
