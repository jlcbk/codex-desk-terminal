#!/usr/bin/env python3
"""ZCode hook dump——Z2 验证工具：把 hook 收到的 stdin 原样落盘。

供 bridge ZCode 适配（docs/P3.6_DESKTOP_OBSERVATION.md §3.3 Z2）探针用。
挂在工作区 .zcode/config.json 的各 hook 事件上，exit 0 且不输出任何内容，
对会话零干扰。输出文件：artifacts/zcode_hooks/dump.jsonl（每行一条事件）。

注意：不落全量环境变量，只记录 CLAUDE_*/ZCODE_* 白名单键，避免凭证进入证据。
"""
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "artifacts", "zcode_hooks")
OUT_FILE = os.path.join(OUT_DIR, "dump.jsonl")
ENV_ALLOW = ("CLAUDE_", "ZCODE_")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw_nonjson": raw[:2000]}

    record = {
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stdin": payload,
        "env": {
            k: v
            for k, v in os.environ.items()
            if k.startswith(ENV_ALLOW)
        },
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # 故意不输出：空输出 + exit 0 = 纯观察，不注入上下文、不阻塞
    return 0


if __name__ == "__main__":
    sys.exit(main())
