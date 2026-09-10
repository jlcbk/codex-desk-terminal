#!/usr/bin/env python3
"""P3.1 live smoke — 真实 codex app-server 受控 turn + 额度读取（不在 pytest 默认集）。

对真实 codex CLI（锁定 0.152.0）做一次最小验证：

1. 启动自己的 stdio app-server（initialize + experimentalApi）
2. 能力检测 + account/rateLimits/read（额度实测）
3. 隔离临时 cwd 中建 ephemeral thread（readOnly 沙箱 + networkAccess=false +
   approvalPolicy=never + 固定模型 gpt-5.6-sol），跑一个受控 turn："只回复 ok"
4. 产物（快照序列、脱敏原始日志、报告）写入 artifacts/codex/
5. 落盘后做脱敏自检（home 路径 / 邮箱 / API key 模式不得出现）

安全红线与 adapter 一致：绝不 resume/触碰已存在 thread；绝不 approve/reject
（审批请求只接收不回应）；会话只在隔离临时目录；每次交互带超时；结束后
清理进程与临时目录。

用法：
    python3 scripts/codex_live_smoke.py [--out DIR] [--codex BIN] [--model M]
返回：adapter 退出码（0 = turn completed；见 bridge/sources/codex.py 退出码表）。
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
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("codex live smoke: codex=%s model=%s prompt=<content len=%d> out=%s"
          % (args.codex, args.model, len(PROMPT), args.out), file=sys.stderr)

    adapter = codex_mod.CodexAdapter(
        prompt=PROMPT, model=args.model, codex_bin=args.codex,
        turn_timeout=args.turn_timeout,
        epoch="codex-smoke-%d" % int(time.time() * 1000),
    )
    result = adapter.run()
    codex_mod.write_artifacts(args.out, result)

    last = result.snapshots[-1] if result.snapshots else {}
    usage = last.get("usage") or {}
    summary = {
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
