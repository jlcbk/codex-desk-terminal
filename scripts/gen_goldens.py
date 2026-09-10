#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_goldens.py — 显式 golden 生成（P2.4 集成，A6）。一次性建 golden 的唯一
授权路径；绝不是回归掩盖工具：check_ui.py 对 golden 永远只读（UI_CONTRACT §2.6），
golden 的最终采纳（入 tests/golden/ 并提交）由 A0 审批。

流程：
  1. 对 tests/fixtures/scenarios/S*.jsonl 逐个跑模拟器确定性回放
     （--scenario --capture-dir；虚拟时钟/固定字体/LVGL 9.3.0，无墙钟无动画）；
  2. 校验每场景产出 manifest 与帧（帧数≥1、manifest 声明帧齐全）；
  3. 把帧拷贝到 golden 目录（默认 artifacts/ui/golden_candidate/；写 tests/golden/
     必须显式 --golden-dir tests/golden，且已存在 golden 时需 --force 覆盖）；
  4. 打印每帧 sha256 与清单，供 A0 审批留档。

用法：
    uv run --python 3.12 python scripts/gen_goldens.py                 # 候选目录
    uv run --python 3.12 python scripts/gen_goldens.py --golden-dir tests/golden [--force]
    uv run --python 3.12 python scripts/gen_goldens.py --no-replay     # 复用已有回放

退出码：0 成功；1 回放或校验失败；2 用法/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SIM = REPO / "build" / "simulator" / "codex-display-sim"
SCEN_DIR = REPO / "tests" / "fixtures" / "scenarios"
DEFAULT_GOLDEN = REPO / "artifacts" / "ui" / "golden_candidate"
DEFAULT_REPLAY = REPO / "artifacts" / "ui" / "replay"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay_all(replay_dir: Path) -> dict[str, Path]:
    if not SIM.is_file():
        print(f"模拟器不存在：{SIM}（先运行 scripts/build_simulator.sh）", file=sys.stderr)
        sys.exit(2)
    if replay_dir.exists():
        shutil.rmtree(replay_dir)
    replay_dir.mkdir(parents=True)
    out: dict[str, Path] = {}
    for scen_file in sorted(SCEN_DIR.glob("S*.jsonl")):
        cmd = [str(SIM), "--scenario", str(scen_file),
               "--capture-dir", str(replay_dir)]
        env = {"SDL_VIDEODRIVER": "dummy", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(REPO), env=env, timeout=300)
        if proc.returncode != 0:
            print(f"回放失败：{scen_file.name}（exit {proc.returncode}）",
                  file=sys.stderr)
            sys.stderr.write(proc.stdout[-2000:] + proc.stderr[-2000:])
            sys.exit(1)
        scen = scen_file.stem
        d = replay_dir / scen
        manifest = d / "manifest.jsonl"
        if not manifest.is_file():
            print(f"回放未产出 manifest：{d}", file=sys.stderr)
            sys.exit(1)
        frames = [ln for ln in manifest.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")
                  and json.loads(ln).get("frame")]
        pngs = sorted(d.glob("*.png"))
        if len(frames) != len(pngs):
            print(f"{scen}: manifest 帧声明 {len(frames)} ≠ 实际帧 {len(pngs)}",
                  file=sys.stderr)
            sys.exit(1)
        out[scen] = d
        print(f"[replay] {scen:<22} {len(pngs):>3} 帧")
    return out


def collect_frames(replay_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for scen in sorted(p.name for p in replay_dir.iterdir() if p.is_dir()):
        d = replay_dir / scen
        if (d / "manifest.jsonl").is_file() and list(d.glob("*.png")):
            out[scen] = d
    if not out:
        print(f"回放目录没有任何场景产物：{replay_dir}", file=sys.stderr)
        sys.exit(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--golden-dir", type=Path, default=DEFAULT_GOLDEN,
                    help="golden 输出目录（默认候选目录；写 tests/golden 为显式 A0 动作）")
    ap.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY,
                    help="回放产物目录（默认 artifacts/ui/replay）")
    ap.add_argument("--no-replay", action="store_true",
                    help="不重新回放，复用 --replay-dir 现有产物")
    ap.add_argument("--force", action="store_true",
                    help="golden 目录已有内容时允许覆盖（候选目录默认直接覆盖）")
    args = ap.parse_args()

    if args.no_replay:
        scens = collect_frames(args.replay_dir)
    else:
        scens = replay_all(args.replay_dir)

    explicit_golden = args.golden_dir == REPO / "tests" / "golden"
    if explicit_golden and args.golden_dir.exists() and any(args.golden_dir.iterdir()):
        if not args.force:
            print("tests/golden 已有内容：覆盖需显式 --force（golden 审批红线）",
                  file=sys.stderr)
            return 2
    elif args.golden_dir.exists() and any(args.golden_dir.iterdir()):
        shutil.rmtree(args.golden_dir)  # 候选目录：静默重建
    args.golden_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    print(f"== 拷贝 golden → {args.golden_dir} ==")
    for scen, d in scens.items():
        for png in sorted(d.glob("*.png")):
            dst = args.golden_dir / png.name
            shutil.copyfile(png, dst)
            if sha256(png) != sha256(dst):
                print(f"拷贝后 sha256 不符：{png.name}", file=sys.stderr)
                return 1
            total += 1
    print(f"共 {total} 帧 / {len(scens)} 场景")

    manifest = args.golden_dir.parent / (args.golden_dir.name + "_SHA256SUMS.txt")
    with manifest.open("w", encoding="utf-8") as fh:
        for png in sorted(args.golden_dir.glob("*.png")):
            fh.write(f"{sha256(png)}  {png.name}\n")
    print(f"sha256 清单：{manifest}")
    if not explicit_golden:
        print("（候选目录；正式采纳请由 A0 审批后 "
              "--golden-dir tests/golden --force 写入并提交）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
