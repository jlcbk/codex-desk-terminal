#!/usr/bin/env python3
"""zcode_hook_spool.py — ZCode hook 写入端（ZC2，A2；ZC3 扩展）：把 hook 事件
最小化记录追加写入 spool 文件，供 bridge ZCode 观察源（bridge/sources/zcode.py，
ZC1）读取。

与探针版 scripts/zcode_hook_dump.py 的差异：本脚本是生产写入端——只落冻结的
七字段行（基础四字段 + ZC3 放行的三个可选字段；绝不落 prompt 内容 / 其他工具
的 tool_input / 环境变量）：

    {"event":"<hook事件名>","session_id":"<sessionId>","tool_name":"<工具名或null>",
     "received_at":"<ISO或null>",
     "cwd":"<stdin 的 cwd 原样或null>",                       ← ZC3 放行
     "summary":"<审批内容摘要（首行、≤400字符）或null>",      ← 仅 PermissionRequest
     "plan":{"total":N,"steps":[{"text","status"}],"truncated":bool}|null}
                                                              ← 仅 TodoWrite
- 事件清单恰 7 个：SessionStart / UserPromptSubmit / PreToolUse /
  PermissionRequest / PostToolUse / PostToolUseFailure / Stop（与
  scripts/install_zcode_hooks.py 两端对齐）。
- ZC3 放行裁决（A0 裁决 + 用户批准，2026-09-12，仅限本机 spool 文件）：
  ① cwd 工作目录路径——观察器取 basename 作 project；② TodoWrite 的 todos
  任务清单（agent 自己的任务计划，非用户提示词）——观察器映射 PLAN 页；
  ③ PermissionRequest 的审批内容摘要——仅 tool_input 里 command/path/url/
  query/file_path 字符串字段的首行、strip、≤400 字符（观察器侧再 scrub+截
  192 上协议），NEEDS YOU 页显示真实审批内容（如 `$ git push origin main`）。
  放行范围仅此三项；其他任何工具的 tool_input、任何 prompt/回复文本仍然绝不
  落盘；凭证类内容任何情况不入 spool。spool 文件权限收紧为 0600。
- spool 路径默认 ~/.zcode/cli/cdt-hook-spool.jsonl，环境变量 CDT_HOOK_SPOOL
  覆盖（服务端 --spool 默认同源，读写两端保持一致）。
- fcntl.flock 独占锁内「查大小→超 1MB 先 truncate→追加一行」，两次快速调用
  不互删；读取端（观察器）按 offset>size 重读语义容错轮转。
- 契约：exit 0、无 stdout、全程 ≤3s（安装器 timeoutMs=3000）；坏 stdin
  （非 JSON）不致命，照样落行（session_id/tool_name 取 null）。
- 向后兼容：cwd/summary/plan 为可选字段（null 表示无）；旧版四字段行对读取
  端仍然合法。
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

# ---- plan 提取边界（ZC3 放行裁决：仅 TodoWrite 一个工具）--------------------
#: 唯一放行 tool_input 的工具名；其他任何工具的输入绝不提取。
PLAN_TOOL_NAME = "TodoWrite"
#: plan 最多保留的 steps 条数（超出截断，truncated=true，total=原始总数）。
PLAN_MAX_STEPS = 8
#: 单条 step 文本的码点安全截断上限（字符数，防 spool 膨胀）。
PLAN_MAX_TEXT_CHARS = 200
#: 原样放行的 status；其余（含缺失/坏类型/activeForm 等）一律归一化为 pending。
_PLAN_KEEP_STATUSES = ("in_progress", "completed")

# ---- summary 提取边界（ZC3 放行裁决：仅 PermissionRequest 一个事件）----------
#: 唯一放行摘要的事件名；其他任何事件的输入绝不提取。
SUMMARY_EVENT_NAME = "PermissionRequest"
#: 依次尝试的 tool_input 字符串字段（Bash 命令/文件路径/URL/搜索词，实测常见键）。
SUMMARY_KEYS = ("command", "path", "url", "query", "file_path")
#: 单条摘要的截断上限（字符数；观察器侧再 scrub+截 192 字节上协议）。
SUMMARY_MAX_CHARS = 400


def extract_plan(event: str, payload: dict):
    """TodoWrite 的 todos 任务清单 → spool plan 字典；其余一律 None。

    放行条件（三者缺一不可，ZC3 裁决）：event=="PreToolUse" 且
    tool_name=="TodoWrite" 且 toolInput.todos 是非空 list。工具输入键按实测
    双命名兼容（tool_input 优先，回退 toolInput）。**绝不提取任何其他工具的
    tool_input**，也绝不提取 prompt/回复文本。

    返回结构（无放行内容时 None）::

        {"total": <原始 todos 条数>,
         "steps": [{"text": <content strip 后 ≤200 字符>, "status": ...}, ...],
         "truncated": <清洗后的 steps 超 8 条被截断>}

    - text：content strip 后非空才保留（空值剔除），Python 字符串切片天然
      码点安全（不劈开多字节字符）；
    - status：in_progress/completed 原样，其余一律 pending；
    - steps ≤8 条（先剔空再截断）；total 恒为原始 todos 条数（含被剔除的
      空条目，如实反映上游规模）。
    """
    if event != "PreToolUse":
        return None
    tool_name = payload.get("tool_name")
    if tool_name != PLAN_TOOL_NAME:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")  # 实测 stdin 双命名兼容
    if not isinstance(tool_input, dict):
        return None
    todos = tool_input.get("todos")
    if not isinstance(todos, list) or not todos:
        return None
    steps = []
    for item in todos:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        status = item.get("status")
        if status not in _PLAN_KEEP_STATUSES:
            status = "pending"
        steps.append({"text": text[:PLAN_MAX_TEXT_CHARS], "status": status})
    return {
        "total": len(todos),
        "steps": steps[:PLAN_MAX_STEPS],
        "truncated": len(steps) > PLAN_MAX_STEPS,
    }


def extract_summary(event: str, payload: dict):
    """PermissionRequest 的审批内容摘要 → 单行字符串；其余一律 None。

    放行条件（ZC3 裁决）：event=="PermissionRequest" 且 tool_input 里
    command/path/url/query/file_path 之一是非空字符串（依次尝试，取第一个
    命中）。摘要=该字段的首行（多行命令只取第一行）、strip、截断
    ≤400 字符（Python 切片码点安全；观察器侧再 scrub_text+截 192 字节才上
    协议）。全部字段缺失/坏类型/空白 → None（观察器回退工具名）。

    **绝不提取其他任何事件的任何字段**，绝不提取 prompt/回复文本；字段值
    只经"取首行+截断"，不做内容判断（脱敏归观察器侧 scrub_text 统一处理）。
    """
    if event != SUMMARY_EVENT_NAME:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")  # 实测 stdin 双命名兼容
    if not isinstance(tool_input, dict):
        return None
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        first_line = value.split("\n", 1)[0].strip()
        if not first_line:
            continue
        return first_line[:SUMMARY_MAX_CHARS]
    return None


def resolve_spool_path(environ: dict | None = None) -> Path:
    """spool 路径：CDT_HOOK_SPOOL 优先，否则 ~/.zcode/cli/cdt-hook-spool.jsonl。"""
    env = os.environ if environ is None else environ
    raw = env.get("CDT_HOOK_SPOOL") or str(DEFAULT_SPOOL)
    return Path(raw).expanduser()


def build_record(event: str, payload: dict, received_at: str | None = None) -> dict:
    """最小化记录（冻结七字段）：基础四字段 + cwd/summary/plan（后三者 ZC3 放行）。

    红线（ZC3 放行裁决 2026-09-12）：新增内容仅限 cwd 工作目录路径（原样）、
    PermissionRequest 的审批内容摘要（extract_summary）与 TodoWrite 的 todos
    任务清单（extract_plan）；stdin 里的 prompt、其他事件/其他工具的
    tool_input、环境变量一概不进入返回值；凭证类内容任何情况不入 spool
    （与凭证不入 fixtures/日志的同一红线）。
    """
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    cwd = payload.get("cwd")
    return {
        "event": event,
        "session_id": session_id if isinstance(session_id, str) else None,
        "tool_name": tool_name if isinstance(tool_name, str) else None,
        "received_at": received_at
        if received_at is not None
        else time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # ZC3：cwd 原样保留（非字符串 → null）；观察器自行取 basename+脱敏。
        "cwd": cwd if isinstance(cwd, str) else None,
        # ZC3：仅 PermissionRequest 的审批内容摘要；其他事件恒 null。
        "summary": extract_summary(event, payload),
        # ZC3：仅 TodoWrite 的 todos；其他工具恒 null。
        "plan": extract_plan(event, payload),
    }


def append_line(path: Path, line: str) -> None:
    """独占锁内追加一行（>1MB 先 truncate 轮转）。

    - flock 覆盖「查大小→轮转→写」全程：两个写入端并发时不会出现
      A 轮转 B 追加、A 再 truncate 把 B 的行删掉的交错（并发不互删）。
    - 追加模式（O_APPEND）保证行总是落在当前文件尾，truncate 后其他
      持锁写入端的行不会被打断。
    - 权限 0600（ZC3：现含 cwd 路径与任务文本，收紧）：创建时 O_CREAT 传
      0o600，且每次打开后 fchmod 强制 0600——不依赖 umask，历史遗留的宽松
      权限文件也在下次写入时被收紧。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:  # 特殊文件系统（如某些挂载点）不支持 fchmod：不致命
        pass
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
