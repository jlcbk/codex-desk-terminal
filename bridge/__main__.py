"""`python -m bridge`：mock 场景 / JSONL 回放 / 真实 codex adapter → 快照输出。

    python -m bridge --source mock --scenario lifecycle --out DIR
    python -m bridge --source mock --scenario lifecycle          # JSONL 打到 stdout
    python -m bridge --source replay --file PATH --out DIR
    python -m bridge --source codex --prompt "只回复 ok" --out DIR --live
    python -m bridge --source codex --prompt "只回复 ok"          # 无 --live 为 dry-run

--out 每份快照写一个 JSON 文件，文件名含递增序号（snapshot_<epoch>_<seq>.json（codex 源；mock/replay 仍为 snapshot_<seq>.json））；
无 --out 时每行一份紧凑 JSON（JSONL）打到 stdout。
退出码：0 成功，2 用法/输入错误；codex source 的其余退出码见
bridge/sources/codex.py（0 completed / 3 failed / 4 interrupted / 5 超时 /
6 能力缺失 / 7 进程反复退出）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .sources import mock, replay


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge",
        description="Codex Desk Terminal bridge (deterministic snapshots; "
                    "mock / replay offline, codex live adapter)",
    )
    p.add_argument("--source", choices=["mock", "replay", "codex"], default="mock",
                   help="event source: built-in mock scenario, JSONL replay, or "
                        "live codex app-server adapter (P3.1)")
    p.add_argument("--scenario", default=None,
                   help="mock scenario name (default: lifecycle); one of: "
                        + ", ".join(mock.SCENARIO_NAMES))
    p.add_argument("--file", default=None,
                   help="JSONL path for --source replay (one NormalizedEvent per line)")
    p.add_argument("--prompt", default=None,
                   help="user prompt for --source codex (required with --live)")
    p.add_argument("--live", action="store_true",
                   help="--source codex: actually spawn codex app-server and run a "
                        "controlled ephemeral turn; without it the command is a "
                        "dry-run that prints the plan and touches nothing")
    p.add_argument("--codex-bin", default="codex",
                   help="codex CLI binary for --source codex (default: codex)")
    p.add_argument("--model", default=None,
                   help="model to pin for --source codex (default: gpt-5.6-sol; "
                        "0.152.0 rejects the account default gpt-6-astra)")
    p.add_argument("--turn-timeout", type=float, default=120.0,
                   help="max seconds to wait for the codex turn to complete (default 120)")
    p.add_argument("--out", default=None,
                   help="output directory: one JSON file per snapshot; "
                        "omit to print JSONL to stdout")
    p.add_argument("--epoch", default=None,
                   help="bridge_epoch id (default: mock-run-001 / replay-001 / codex-<wallms>)")
    p.add_argument("--anchor-ms", type=int, default=0,
                   help="UTC anchor for *_at_ms mapping in replay source "
                        "(mock source always uses its fixed anchor); default: 0")
    return p


def _dump_line(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.source == "codex":
        from .sources import codex as codex_source
        if args.live and not args.prompt:
            print("bridge: --source codex --live requires --prompt", file=sys.stderr)
            return 2
        return codex_source.run_cli(args)

    if args.source == "mock":
        name = args.scenario if args.scenario is not None else mock.SCENARIO_LIFECYCLE
        try:
            snapshots = mock.run(name, epoch=args.epoch or mock.DEFAULT_EPOCH)
        except ValueError as exc:
            print(f"bridge: {exc}", file=sys.stderr)
            return 2
    else:
        if not args.file:
            print("bridge: --source replay requires --file PATH", file=sys.stderr)
            return 2
        if not os.path.isfile(args.file):
            print(f"bridge: file not found: {args.file}", file=sys.stderr)
            return 2
        try:
            snapshots = replay.run_file(
                args.file, epoch=args.epoch or replay.DEFAULT_EPOCH,
                utc_anchor_ms=args.anchor_ms)
        except ValueError as exc:
            print(f"bridge: {exc}", file=sys.stderr)
            return 2

    if args.out is None:
        for snap in snapshots:
            sys.stdout.write(_dump_line(snap) + "\n")
    else:
        os.makedirs(args.out, exist_ok=True)
        for snap in snapshots:
            path = os.path.join(args.out, f"snapshot_{snap['seq']:04d}.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_dump_line(snap) + "\n")
        print(
            f"bridge: wrote {len(snapshots)} snapshots to {args.out} "
            f"(source={args.source}, scenario={args.scenario or '-'}, file={args.file or '-'})",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
