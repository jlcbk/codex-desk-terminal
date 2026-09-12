#!/usr/bin/env python3
"""zcode_hook_spool.py — ZCode hook 写入端（ZC2，A2）：把 hook 事件最小化记录
追加写入 spool 文件，供 bridge ZCode 观察源（bridge/sources/zcode.py，ZC1）读取。

与探针版 scripts/zcode_hook_dump.py 的差异：本脚本是生产写入端——只落冻结的
四字段行（绝不落 prompt 内容 / tool_input / 环境变量）：

    {"event":"<hook事件名>","session_id":"<sessionId>","tool_name":"<工具名或null>",
     "received_at":"<ISO或null>"}

- 事件清单恰 7 个：SessionStart / UserPromptSubmit / PreToolUse /
  PermissionRequest / PostToolUse / PostToolUseFailure / Stop（与
  scripts/install_zcode_hooks.py 两端对齐）。
- spool 路径默认 ~/.zcode/cli/cdt-hook-spool.jsonl，环境变量 CDT_HOOK_SPOOL
  覆盖（服务端 --spool 默认同源，读写两端保持一致）。
- fcntl.flock 独占锁内「查大小→超 1MB 先 truncate→追加一行」，两次快速调用
  不互删；读取端（观察器）按 offset>size 重读语义容错轮转。
- 契约：exit 0、无 stdout、全程 ≤3s（安装器 timeoutMs=3000）；坏 stdin
  （非 JSON）不致命，照样落行（session_id/tool_name 取 null）。
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

#: 冻结的 7 个 hook 事件（与安装器 HOOK_EVENTS 一致；两端的对齐锚点）。
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)

#: spool 轮转阈值：超过即先 truncate 再追加（INTERFACES：长驻有界）。
MAX_SPOOL_BYTES = 1024 * 1024

#: 默认 spool 路径（CDT_HOOK_SPOOL 可覆盖）。
DEFAULT_SPOOL = Path("~/.zcode/cli/cdt-hook-spool.jsonl")


def resolve_spool_path(environ: dict | None = None) -> Path:
    """spool 路径：CDT_HOOK_SPOOL 优先，否则 ~/.zcode/cli/cdt-hook-spool.jsonl。"""
    env = os.environ if environ is None else environ
    raw = env.get("CDT_HOOK_SPOOL") or str(DEFAULT_SPOOL)
    return Path(raw).expanduser()


def build_record(event: str, payload: dict, received_at: str | None = None) -> dict:
    """最小化记录（冻结四字段）：事件名 + 会话 id + 工具名 + 接收时间。

    红线：只取 session_id / tool_name 两个标量；stdin 里的 prompt、tool_input、
    环境变量一概不进入返回值（凭证不入 fixtures/日志的同一红线）。
    """
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    return {
        "event": event,
        "session_id": session_id if isinstance(session_id, str) else None,
        "tool_name": tool_name if isinstance(tool_name, str) else None,
        "received_at": received_at
        if received_at is not None
        else time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def append_line(path: Path, line: str) -> None:
    """独占锁内追加一行（>1MB 先 truncate 轮转）。

    - flock 覆盖「查大小→轮转→写」全程：两个写入端并发时不会出现
      A 轮转 B 追加、A 再 truncate 把 B 的行删掉的交错（并发不互删）。
    - 追加模式（O_APPEND）保证行总是落在当前文件尾，truncate 后其他
      持锁写入端的行不会被打断。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            if f.tell() > MAX_SPOOL_BYTES:
                f.truncate(0)
            f.write(line if line.endswith("\n") else line + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None, *, stdin=None,
         environ: dict | None = None) -> int:
    """hook 入口：exit 0、无 stdout；任何失败都不干扰会话（只 stderr 备注）。"""
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        event = argv[0] if argv else ""
        if event not in HOOK_EVENTS:
            # 防御：未注册的事件名不落盘（避免污染 spool），仍 exit 0。
            return 0
        raw = (sys.stdin if stdin is None else stdin).read()
        try:
            payload = json.loads(raw) if raw and raw.strip() else {}
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        record = build_record(event, payload)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        append_line(resolve_spool_path(environ), line)
    except Exception as exc:  # noqa: BLE001 — hook 绝不因自身故障阻塞会话
        print("zcode_hook_spool: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
