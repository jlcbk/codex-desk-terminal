"""`python -m bridge`：mock 场景 / JSONL 回放 → 快照输出（命令名对齐 DEVELOPMENT_PLAN §8）。

    python -m bridge --source mock --scenario lifecycle --out DIR
    python -m bridge --source mock --scenario lifecycle          # JSONL 打到 stdout
    python -m bridge --source replay --file PATH --out DIR

--out 每份快照写一个 JSON 文件，文件名含递增序号（snapshot_<seq>.json）；
无 --out 时每行一份紧凑 JSON（JSONL）打到 stdout。退出码：0 成功，2 用法/输入错误。
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
        description="Codex Desk Terminal mock/replay bridge (deterministic snapshots)",
    )
    p.add_argument("--source", choices=["mock", "replay"], default="mock",
                   help="event source: built-in mock scenario or JSONL replay")
    p.add_argument("--scenario", default=None,
                   help="mock scenario name (default: lifecycle); one of: "
                        + ", ".join(mock.SCENARIO_NAMES))
    p.add_argument("--file", default=None,
                   help="JSONL path for --source replay (one NormalizedEvent per line)")
    p.add_argument("--out", default=None,
                   help="output directory: one JSON file per snapshot; "
                        "omit to print JSONL to stdout")
    p.add_argument("--epoch", default=None,
                   help="bridge_epoch id (default: mock-run-001 / replay-001)")
    p.add_argument("--anchor-ms", type=int, default=0,
                   help="UTC anchor for *_at_ms mapping in replay source "
                        "(mock source always uses its fixed anchor); default: 0")
    return p


def _dump_line(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

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
