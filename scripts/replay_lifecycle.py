#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_lifecycle.py — P2.5 完整生命周期回放 + 自动断言（A6）。

链路（P2.5 验收：五个阶段均有快照；PLAN UPDATE 保持 WORKING；最终低压页出现；
生命周期须检查中间每一步，不只最后一帧——DEVELOPMENT_PLAN §8/§5 P2.5 行）：

  1. bridge replay 源（P1.2 交付）产出业务快照：
       python -m bridge --source replay --file tests/fixtures/bridge/lifecycle_events.jsonl
         --epoch lifecycle-001 --anchor-ms 1789000000000 --out artifacts/bridge_lifecycle
  2. 对齐校验：tests/fixtures/scenarios/S_lifecycle.jsonl 的全部 app_state payload
     与上述快照逐 seq 逐字段一致（fixture 由 scripts/gen_scenarios.py 从同一
     replay 源生成；本步骤证明回放链路未被手改）。
  3. 模拟器确定性回放 S_lifecycle.jsonl → 帧 + manifest
     （SDL_VIDEODRIVER=dummy，虚拟时钟，无墙钟）。
  4. check_ui 像素回归 + 语义断言（--golden tests/golden --scenario S_lifecycle）。
  5. 五阶段 checkpoint 逐帧断言（页面/状态词/文本/时长，含否定断言），
     checkpoint 清单 = S_lifecycle.jsonl 头部声明（UI_CONTRACT §4.1）。

退出码：0 全部通过；1 任一断言失败；2 环境/用法错误。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
T0 = 1789000000000
SCEN = REPO / "tests" / "fixtures" / "scenarios" / "S_lifecycle.jsonl"
EVENTS = REPO / "tests" / "fixtures" / "bridge" / "lifecycle_events.jsonl"
SIM = REPO / "build" / "simulator" / "codex-display-sim"
BRIDGE_OUT = REPO / "artifacts" / "bridge_lifecycle"
REPLAY_OUT = REPO / "artifacts" / "ui" / "lifecycle_replay"

sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

FAILS: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def main() -> int:
    if not SIM.is_file():
        print(f"模拟器不存在：{SIM}", file=sys.stderr)
        return 2

    # ---- 1. bridge replay 源产出快照（P1.2 交付的 --source replay）----
    BRIDGE_OUT.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "bridge", "--source", "replay",
           "--file", str(EVENTS), "--epoch", "lifecycle-001",
           "--anchor-ms", str(T0), "--out", str(BRIDGE_OUT)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if proc.returncode != 0:
        print(f"bridge replay 失败（exit {proc.returncode}）：{proc.stderr[-800:]}",
              file=sys.stderr)
        return 1
    snaps = [json.loads(p.read_text(encoding="utf-8"))
             for p in sorted(BRIDGE_OUT.glob("snapshot_*.json"))]
    print(f"[1/5] bridge replay 源：{len(snaps)} 份快照 → {BRIDGE_OUT}")

    # ---- 2. S_lifecycle fixture 的 app_state 与 replay 快照对齐校验 ----
    actions = [json.loads(ln) for ln in SCEN.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith("#")]
    scen_states = [a["payload"] for a in actions if a["action"] == "app_state"]
    check(scen_states == snaps,
          f"[2/5] S_lifecycle app_state 与 bridge replay 快照逐字段一致（{len(scen_states)} 份）",
          "fixture 与 replay 输出出现分歧——请用 scripts/gen_scenarios.py 重新生成")

    # ---- 3. 模拟器确定性回放 ----
    if REPLAY_OUT.exists():
        import shutil
        shutil.rmtree(REPLAY_OUT)
    proc = subprocess.run(
        [str(SIM), "--scenario", str(SCEN), "--capture-dir", str(REPLAY_OUT)],
        capture_output=True, text=True, cwd=str(REPO),
        env={"SDL_VIDEODRIVER": "dummy", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    if proc.returncode != 0:
        print(f"模拟器回放失败：{proc.stderr[-800:]}", file=sys.stderr)
        return 1
    manifest = [json.loads(ln)
                for ln in (REPLAY_OUT / "S_lifecycle" / "manifest.jsonl")
                .read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    frames = [m for m in manifest if m.get("frame")]
    print(f"[3/5] 模拟器回放：{len(manifest)} actions，{len(frames)} 帧 → "
          f"{REPLAY_OUT}/S_lifecycle/")

    def rec(tag: str) -> dict:
        for m in manifest:
            if m.get("tag") == tag:
                return m
        return {}

    def view(tag: str) -> dict:
        return rec(tag).get("view") or {}

    # ---- 4. check_ui 像素回归 + 语义断言 ----
    rc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_ui.py"),
         "--golden", str(REPO / "tests" / "golden"),
         "--actual", str(REPLAY_OUT), "--scenario", "S_lifecycle"],
        capture_output=True, text=True).returncode
    check(rc == 0, "[4/5] check_ui：golden 像素零差异 + 语义断言（S_lifecycle）",
          f"check_ui 退出码 {rc}")

    # ---- 5. 五阶段 checkpoint 断言（页面/文本/时长；不只验末帧）----
    print("[5/5] 五阶段 checkpoint 断言：")
    stages = [
        ("idle", "阶段① idle 起始快照"),
        ("working", "阶段② working"),
        ("plan_update", "阶段③ PLAN UPDATE"),
        ("needs_you", "阶段④ needs_you"),
        ("done", "阶段⑤ done 终态"),
        ("low_battery", "阶段⑥ 最终低压页（独立电池 trace 驱动）"),
    ]
    for tag, name in stages:
        r = rec(tag)
        check(bool(r), f"checkpoint {tag} 存在（{name}）")
    v = view("idle")
    check(v.get("status") == "IDLE" and v.get("page") == "now",
          "阶段① IDLE：NOW 页状态词 IDLE", str(v.get("status")))
    v = view("working")
    check(v.get("status") == "WORKING" and v.get("page") == "now",
          "阶段② WORKING：NOW 页状态词 WORKING", str(v.get("status")))
    # 阶段③ PLAN UPDATE：内容事件，状态保持 WORKING
    v = view("plan_update")
    check(v.get("status") == "WORKING",
          "阶段③ PLAN UPDATE 后状态词保持 WORKING", str(v.get("status")))
    check(v.get("plan") == "PLAN 1/3",
          "阶段③ PLAN 摘要更新为 1/3（内容生效）", str(v.get("plan")))
    v = view("needs_you")
    check(v.get("status") == "NEEDS YOU" and v.get("attention_present"),
          "阶段④ NEEDS YOU：等待提醒可见", f"{v.get('status')}")
    check(v.get("page") == "now",
          "阶段④ 业务页仍为 NOW（低压未介入）", str(v.get("page")))
    # 阶段⑤ DONE：终态、pending 清零、时长定格（fresh 但不再推进）
    v = view("done")
    check(v.get("status") == "DONE" and not v.get("attention_present"),
          "阶段⑤ DONE：pending 清零", f"{v.get('status')}")
    check(v.get("elapsed") == "00:02" and v.get("frozen") is False,
          "阶段⑤ 终态时长定格在快照基值 00:02（终态不推进）",
          f"elapsed={v.get('elapsed')}")
    done_states = [m["view"]["elapsed"] for m in manifest
                   if m.get("view") and m["at_ms"] >= 2100
                   and m["view"].get("status") == "DONE"
                   and m["view"].get("page") == "now"]
    check(len(set(done_states)) == 1,
          "阶段⑤ 时长断言：DONE 期间所有帧时长一致（不推进）", str(set(done_states)))
    # 阶段⑥ 最终低压页（电池 trace 独立驱动，Power FSM 判定）
    v = view("low_battery")
    check(v.get("page") == "low_battery" and v.get("forced"),
          "阶段⑥ 最终低压页出现（low_battery 强制页）", str(v.get("page")))
    check(v.get("voltage") == "3.60V",
          "阶段⑥ 低压页显示电压 3.60V", str(v.get("voltage")))
    check(v.get("status") == "LOW BATTERY",
          "阶段⑥ 状态词 LOW BATTERY", str(v.get("status")))
    # KEY 语义：短按被拒（无帧且仍停留）、长按只静音不解除
    kr, km = rec("key_rejected"), view("key_long_mute")
    check(kr is not None and kr.get("frame") is None
          and kr["view"].get("page") == "low_battery",
          "短按被拒：无新帧且仍停留低压页")
    check(km.get("muted") is True and km.get("page") == "low_battery",
          "长按只静音（MUTED）且不解除强制页")
    # 时长节拍：working 阶段 100ms→900ms 间运行时长按快照基值推进
    el = [(m["at_ms"], m["view"]["elapsed"]) for m in manifest
          if m.get("view") and m["at_ms"] in (100, 900)
          and m["view"].get("page") == "now"]
    check(len(el) == 2 and el[0][1] == "00:00" and el[1][1] == "00:00",
          "运行时长按快照基值显示（100ms/900ms 基值 0s）", str(el))

    print()
    total = 0
    passed = 0
    for line in FAILS:
        total += 0
    # 汇总（重跑一遍计数）
    print(f"汇总: {'全部通过' if not FAILS else f'{len(FAILS)} 项失败'}")
    if FAILS:
        for f in FAILS:
            print(f"  - {f}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
