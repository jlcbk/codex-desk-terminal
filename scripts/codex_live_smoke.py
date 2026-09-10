#!/usr/bin/env python3
"""P3.1/P3.2 live smoke — 真实 codex app-server 受控 turn（不在 pytest 默认集）。

对真实 codex CLI（锁定 0.152.0）做最小验证：

1. 启动自己的 stdio app-server（initialize + experimentalApi）
2. 能力检测 + account/rateLimits/read（额度实测）
3. 隔离临时 cwd 中建 ephemeral thread（readOnly 沙箱 + networkAccess=false +
   approvalPolicy=never + 固定模型 gpt-5.6-sol），跑一个受控 turn（默认 "只回复 ok"）
4. 产物（快照序列、脱敏原始日志、报告）写入 artifacts/codex/
5. 落盘后做脱敏自检（home 路径 / 邮箱 / API key 模式不得出现）

P3.2 场景（映射收尾的 live 实证）：

- 默认      ：受控 turn 完成（P3.1 基线，completed 路径）。
- --interrupt-after S：turn 启动 S 秒后仍无终态就 interrupt 自己的 turn
  （取消路径：turn/completed interrupted → idle+cancelled）。
- 自定义 --prompt ：如 plan 实证（"分三步…" 触发 turn/plan/updated）或
  user-input 实证（要求模型调用 requestUserInput / 触发 waitingOnUserInput）。

安全红线与 adapter 一致：绝不 resume/触碰已存在 thread；绝不 approve/reject/answer
（server→client 请求只接收不回应，等待时只 interrupt 自己的 turn）；会话只在隔离
临时目录；每次交互带超时；结束后清理进程与临时目录。

用法：
    python3 scripts/codex_live_smoke.py [--out DIR] [--codex BIN] [--model M]
        [--prompt TEXT] [--interrupt-after S] [--label NAME] [--turn-timeout S]
返回：adapter 退出码（见 bridge/sources/codex.py 退出码表：
0 completed / 3 failed / 4 interrupted(=cancelled) / 5 超时无终态 /
6 能力缺失 / 7 进程反复退出 / 8 脱敏自检失败）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bridge.redact import CODEX_HOME  # noqa: E402
from bridge.sources import codex as codex_mod  # noqa: E402

DEFAULT_OUT = os.path.join(REPO_ROOT, "artifacts", "codex", "live")
PROMPT = "只回复 ok"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 与 bridge/redact.py 相同的长阈值（真实凭证 ≥20 字符），避免 "desk-terminal"
# 里的 "sk-" 触发自检误报（首次实测曾因此退出码 8，已收紧）。
_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}|api[_-]?key\s*[=:]\s*\S+|token\s*[=:]\s*\S+",
    re.IGNORECASE,
)


def redaction_selfcheck(out_dir: str) -> list:
    """扫描落盘产物；返回违规列表（空 = 通过）。"""
    violations = []
    needles = [
        ("home-path", CODEX_HOME),
        ("email", None),      # 用正则
        ("api-key", None),
    ]
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if CODEX_HOME and CODEX_HOME in text:
            violations.append("%s: contains home path" % name)
        if _EMAIL_RE.search(text):
            violations.append("%s: contains email-like string" % name)
        if _SECRET_RE.search(text):
            violations.append("%s: contains key-like string" % name)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="artifact directory (default: artifacts/codex/live)")
    parser.add_argument("--codex", default="codex", help="codex CLI binary")
    parser.add_argument("--model", default=codex_mod.DEFAULT_MODEL,
                        help="model to pin (0.152.0 rejects the account default)")
    parser.add_argument("--prompt", default=PROMPT,
                        help="controlled turn prompt (default: 只回复 ok)")
    parser.add_argument("--interrupt-after", type=float, default=0.0, dest="interrupt_after",
                        help="if the turn has no terminal status after S seconds, "
                             "interrupt our own turn (cancel-path evidence); 0 = off")
    parser.add_argument("--collab-mode", default=None, dest="collab_mode",
                        choices=["plan", "default"],
                        help="turn/start collaborationMode (schema ModeKind); "
                             "'plan' induces the model's plan tool (P3.2 evidence)")
    parser.add_argument("--thread-config", default=None, dest="thread_config",
                        help="JSON object merged into thread/start `config` "
                             "(e.g. feature flags); never used to pass credentials")
    parser.add_argument("--label", default=None, dest="run_label",
                        help="run label recorded in report.json (evidence metadata)")
    parser.add_argument("--task-label", default="P3.2", dest="task_label",
                        help="task id recorded in report.json")
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("codex live smoke: codex=%s model=%s prompt=<content len=%d> "
          "interrupt_after=%s out=%s"
          % (args.codex, args.model, len(args.prompt), args.interrupt_after,
             args.out), file=sys.stderr)

    thread_config = None
    if args.thread_config:
        try:
            thread_config = json.loads(args.thread_config)
        except ValueError as exc:
            print("bridge: --thread-config is not valid JSON: %s" % exc, file=sys.stderr)
            return 2
        if not isinstance(thread_config, dict):
            print("bridge: --thread-config must be a JSON object", file=sys.stderr)
            return 2

    adapter = codex_mod.CodexAdapter(
        prompt=args.prompt, model=args.model, codex_bin=args.codex,
        turn_timeout=args.turn_timeout, interrupt_after_s=args.interrupt_after,
        collaboration_mode=args.collab_mode, thread_config=thread_config,
        task_label=args.task_label, run_label=args.run_label,
        epoch="codex-smoke-%d" % int(time.time() * 1000),
    )
    result = adapter.run()
    codex_mod.write_artifacts(args.out, result)

    last = result.snapshots[-1] if result.snapshots else {}
    usage = last.get("usage") or {}
    summary = {
        "run_label": args.run_label,
        "exit_code": result.exit_code,
        "exit_reason": result.exit_reason,
        "bridge_epoch": result.report["bridge_epoch"],
        "capabilities": result.report["capabilities"],
        "turn_status": (result.report["attempts"] or [{}])[-1].get("turn_status"),
        "state_flow": codex_mod.state_flow(result.snapshots),
        "usage_measured": usage.get("windows"),
        "usage_available": usage.get("available"),
        "snapshot_count": len(result.snapshots),
        "out_dir": "~/" + os.path.relpath(args.out, CODEX_HOME)
        if CODEX_HOME and args.out.startswith(CODEX_HOME) else args.out,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    violations = redaction_selfcheck(args.out)
    if violations:
        print("REDACTION SELFCHECK FAILED:", file=sys.stderr)
        for item in violations:
            print("  -", item, file=sys.stderr)
        return 8
    print("redaction selfcheck: PASS (%d files scanned)"
          % len(os.listdir(args.out)), file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
