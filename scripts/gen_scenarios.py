#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_scenarios.py — 生成 tests/fixtures/scenarios/ 的 S01–S21 与正式 S_lifecycle
注入 fixtures（P2.4/P2.5 集成，A6）。

场景与断言真源：tests/SCENARIOS.md §3；行格式契约：tests/UI_CONTRACT.md §3。
本脚本只负责把已定稿的场景落成 JSONL 文件（确定性、可重跑、逐字节可复现）；
不定义新场景、不改断言。

对齐声明（P2.5 范围，见 STATUS P1.2 阻塞项「S04/S09/S21 fixture 对齐归 P2.5」）：
  - S04/S09/S21 按 SCENARIOS §3 骨架定稿；业务内容词汇与 P1.2 Mock Bridge
    （bridge/sources/mock.py、tests/fixtures/bridge/lifecycle_events.jsonl）一致。
  - 正式 S_lifecycle 的全部 app_state 行 = `bridge --source replay --file
    tests/fixtures/bridge/lifecycle_events.jsonl --anchor-ms T0` 的快照原文
    （在进程内调用 replay.run_events 生成，逐字段不经手改）；battery trace 按
    §7.2 连续性（临界段 1Hz，间隔 ≤2s）由真实 Power FSM 判定，无画页捷径。
  - 与 SCENARIOS golden 命名列的已知偏差（回放器「无变化不出帧」的物理结果，
    汇报给 A0/A5 折衷）：无 ViewModel 变化的断言帧（如 S09 短按被拒、S10 范围外
    采样与 invalid 采样像素相同）不产出 golden，由 manifest 的 view 语义断言
    覆盖；多帧场景实际帧集 ⊇ SCENARIOS 断言帧清单（额外帧多为导航中间帧）。

用法：uv run --python 3.12 python scripts/gen_scenarios.py [--check]
字体子集守护：所有 fixture 中文必须已被子集覆盖（scripts/gen_font_noto_sc.py
collect_codepoints），emoji 白名单仅 S17 一个（预期渲染为可见 '?'）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

from gen_font_noto_sc import collect_codepoints  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "scenarios"
T0 = 1789000000000  # SCENARIOS §2 固定虚构 UTC 基准
EMOJI_ALLOW = {"\U0001F680"}  # S17 的 1 个 emoji（字体无字形 → 可见替代符）


# ---------------------------------------------------------------- AppState 构造

def step(text: str, status: str) -> dict:
    return {"text": text, "status": status}


def plan(total: int, steps: list, truncated: bool = False) -> dict:
    return {"total": total, "truncated": truncated, "steps": steps}


def attention(count: int, summary: str) -> dict:
    return {"pending_count": count, "summary": summary}


def context(used=None, cap=None, pct=None) -> dict:
    return {"used_tokens": used, "capacity_tokens": cap, "used_percent": pct}


def window(win_id: str, label: str, pct: float, mins: int, resets: int) -> dict:
    return {"id": win_id, "label": label, "used_percent": pct,
            "duration_mins": mins, "resets_at_ms": resets}


def usage(windows: list, updated: int = T0, available: bool = True) -> dict:
    return {"available": available, "updated_at_ms": updated,
            "windows_total": len(windows), "windows_truncated": False,
            "windows": windows}


def thread(tid: str, project: str, state: str, activity: str, updated: int,
           elapsed: int = 0, waiting: int = 0, end_reason=None,
           attn=None, plan_v=None, ctx=None, turn: str = "turn-001") -> dict:
    return {
        "id": tid, "turn_id": turn, "project": project, "state": state,
        "activity": activity, "updated_at_ms": updated,
        "elapsed_ms": elapsed, "waiting_ms": waiting,
        "end_reason": end_reason, "attention": attn,
        # schema：plan 不为 null（steps 可为空数组）
        "plan": plan_v if plan_v is not None else plan(0, []),
        "context": ctx if ctx is not None else context(),
    }


def state_payload(seq: int, threads: list, *, gen_ms: int, epoch: str,
                  selected=None, total: int | None = None,
                  truncated: bool = False, usage_v=None, stale: bool = False) -> dict:
    return {
        "schema_version": 1, "kind": "state", "bridge_epoch": epoch, "seq": seq,
        "generated_at_ms": gen_ms,
        "source": {"kind": "mock", "connected": True, "stale": stale,
                   "last_event_at_ms": gen_ms},
        "selected_thread_id": selected,
        "threads_total": len(threads) if total is None else total,
        "threads_truncated": truncated,
        "threads": threads,
        "usage": usage_v if usage_v is not None else usage([
            window("codex-primary", "300 MIN WINDOW", 42.0, 300, T0 + 3600000)]),
    }


def battery(mv, valid: bool) -> dict:
    return {"battery_mv": mv, "battery_valid": valid}


# ---------------------------------------------------------------- 文件写出

def check_text(s: str) -> None:
    for ch in s:
        if ord(ch) < 0x80 or ch in EMOJI_ALLOW:
            continue
        if ord(ch) not in COVERED:
            raise SystemExit(f"字符 U+{ord(ch):04X} {ch!r} 不在字体子集内：{s!r}")


def dump_line(at: int, action: str, payload, tag: str | None = None) -> str:
    obj = {"at_ms": at, "action": action, "payload": payload}
    if tag:
        obj = {"at_ms": at, "action": action, "tag": tag, "payload": payload}
    for k, v in obj.items():
        if isinstance(v, str):
            check_text(v)
        if k == "payload" and isinstance(v, dict):
            for thread_v in v.get("threads", []):
                for field in ("project", "activity"):
                    check_text(thread_v.get(field, ""))
                if thread_v.get("attention"):
                    check_text(thread_v["attention"].get("summary", ""))
                plan_v = thread_v.get("plan") or {}
                for st in plan_v.get("steps", []):
                    check_text(st.get("text", ""))
            for w in v.get("usage", {}).get("windows", []):
                check_text(w.get("label", ""))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_scenario(name: str, header: list[str], lines: list[str],
                   check_monotonic: bool = True) -> None:
    prev = -1
    for ln in lines:
        at = json.loads(ln)["at_ms"]
        if check_monotonic and at < prev:
            raise SystemExit(f"{name}: at_ms 回退 at {at}")
        prev = at
    text = "\n".join(header) + "\n" + "\n".join(lines) + "\n"
    (OUT / name).write_text(text, encoding="utf-8")
    print(f"写出 {OUT / name} ({len(lines)} actions)")


# ---------------------------------------------------------------- 常用构件

EPOCH = "scenario-001"

WORKING_V1 = plan(3, [step("检查需求", "completed"),
                      step("实现界面", "in_progress"),
                      step("运行测试", "pending")])


def base_working(seq: int = 1, gen_ms: int = T0, activity: str = "正在运行构建与测试",
                 plan_v=WORKING_V1, **kw) -> dict:
    return state_payload(seq, [
        thread("thread-demo", "codex-desk-terminal", "working", activity, gen_ms,
               elapsed=0, plan_v=plan_v),
    ], gen_ms=gen_ms, epoch=kw.pop("epoch", EPOCH), selected="thread-demo", **kw)


# ---------------------------------------------------------------- S01–S21

def build_all() -> None:
    # S01 idle：无任务（threads 空、total 0、无选中）；无 battery 注入 → 电压 --
    write_scenario("S01_idle.jsonl", [
        "# S01_idle — 无任务 IDLE（SCENARIOS §3 S01）",
        f"# 基准：无线程；电压未注入显示 --；额度照常显示。虚拟时钟从 0 起；T0={T0}。",
    ], [dump_line(0, "app_state",
                  state_payload(1, [], gen_ms=T0, epoch=EPOCH, selected=None,
                                total=0, usage_v=usage([
                                    window("codex-primary", "300 MIN WINDOW", 42.0,
                                           300, T0 + 3600000)])),
                  tag="idle")])

    # S02 thinking：+60s 运行时长推进
    write_scenario("S02_thinking.jsonl", [
        "# S02_thinking — THINKING 状态词 + 运行时长节律推进（SCENARIOS §3 S02）",
    ], [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "thinking",
                   "正在分析修改方案与影响面", T0),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="thinking"),
        # Bridge 每 15s 发全量存活快照（INTERFACES §4）；30s 一拍防止 45s 自然 stale
        dump_line(30000, "app_state", state_payload(2, [
            thread("thread-demo", "codex-desk-terminal", "thinking",
                   "正在分析修改方案与影响面", T0 + 30000, elapsed=30000),
        ], gen_ms=T0 + 30000, epoch=EPOCH, selected="thread-demo"), tag="keepalive"),
        dump_line(60000, "advance_time", {"to_ms": 60000}, tag="elapsed60s"),
    ])

    # S03 working：PLAN 摘要 + 当前步骤
    write_scenario("S03_working.jsonl", [
        "# S03_working — WORKING + plan v1（1 completed/1 in_progress/1 pending）",
    ], [dump_line(0, "app_state", base_working(), tag="working")])

    # S04 plan_update：plan v1 → v2（PLAN UPDATE 是内容事件，状态保持 WORKING）
    write_scenario("S04_plan_update.jsonl", [
        "# S04_plan_update — PLAN UPDATE 前后（SCENARIOS §3 S04；内容与 P1.2 mock 词汇对齐）",
        "# 断言：v2 后状态词仍 WORKING；PLAN 完成数/步骤更新且旧计划不残留。",
    ], [
        dump_line(0, "app_state", base_working(seq=1), tag="before"),
        dump_line(1000, "app_state", state_payload(2, [
            thread("thread-demo", "codex-desk-terminal", "working",
                   "实现界面步骤完成，进入测试", T0 + 1000, elapsed=1000,
                   plan_v=plan(4, [step("检查需求", "completed"),
                                   step("实现界面", "completed"),
                                   step("运行测试", "in_progress"),
                                   step("编写文档", "pending")]),
                   ctx=context(12000, 200000, 6.0)),
        ], gen_ms=T0 + 1000, epoch=EPOCH, selected="thread-demo"), tag="after"),
    ])

    # S05 needs_you：粗框反白 + 等待提醒 + 等待时长推进
    write_scenario("S05_needs_you.jsonl", [
        "# S05_needs_you — NEEDS YOU 反白粗框 + 等待时长（SCENARIOS §3 S05）",
    ], [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "needs_you",
                   "等待批准运行命令", T0,
                   attn=attention(1, "运行命令需要批准")),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="needs_you"),
        dump_line(30000, "app_state", state_payload(2, [
            thread("thread-demo", "codex-desk-terminal", "needs_you",
                   "等待批准运行命令", T0 + 30000, elapsed=30000, waiting=30000,
                   attn=attention(1, "运行命令需要批准")),
        ], gen_ms=T0 + 30000, epoch=EPOCH, selected="thread-demo"), tag="keepalive"),
        dump_line(60000, "advance_time", {"to_ms": 60000}, tag="waiting60s"),
    ])

    # S06 done：终态后时长不再推进（presenter 终态冻结）
    write_scenario("S06_done.jsonl", [
        "# S06_done — DONE 终态：pending 清零、粗框消失、时长定格（SCENARIOS §3 S06）",
    ], [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "done", "任务已完成",
                   T0, elapsed=0, end_reason="completed",
                   plan_v=plan(4, [step("检查需求", "completed"),
                                   step("实现界面", "completed"),
                                   step("运行测试", "completed"),
                                   step("编写文档", "completed")],
                               ),
                   ctx=context(15800, 200000, 7.9)),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="done"),
        # 存活快照（保持 fresh；终态 elapsed 不变）；advance 60s 后像素仍不变 →
        # frozen 断言由 manifest（无新帧 + elapsed 不变）覆盖，无 f002 golden
        dump_line(30000, "app_state", state_payload(2, [
            thread("thread-demo", "codex-desk-terminal", "done", "任务已完成",
                   T0 + 30000, elapsed=0, end_reason="completed",
                   plan_v=plan(4, [step("检查需求", "completed"),
                                   step("实现界面", "completed"),
                                   step("运行测试", "completed"),
                                   step("编写文档", "completed")],
                               ),
                   ctx=context(15800, 200000, 7.9)),
        ], gen_ms=T0 + 30000, epoch=EPOCH, selected="thread-demo"), tag="keepalive"),
        dump_line(60000, "advance_time", {"to_ms": 60000}, tag="frozen60s"),
    ])

    # S07 error：超长错误文案裁剪不越界
    long_err = "编译错误：类型不匹配，请检查需求并运行测试，更新文档后整理测试报告并继续后续任务"
    write_scenario("S07_error.jsonl", [
        "# S07_error — ERROR 状态词 + 超长错误文本裁剪（SCENARIOS §3 S07）",
    ], [dump_line(0, "app_state", state_payload(1, [
        thread("thread-demo", "codex-desk-terminal", "error", long_err, T0,
               end_reason="failed"),
    ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="error")])

    # S08 cancelled：needs_you 前置帧 → idle + CANCELLED（粗框消失）
    write_scenario("S08_cancelled.jsonl", [
        "# S08_cancelled — 等待被明确取消终态解除（SCENARIOS §3 S08）",
    ], [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "needs_you",
                   "批量重命名文件", T0,
                   attn=attention(1, "运行命令需要批准"),
                   turn="turn-c1"),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="needs_you"),
        dump_line(1000, "app_state", state_payload(2, [
            thread("thread-demo", "codex-desk-terminal", "idle",
                   "批量重命名文件", T0 + 1000, elapsed=1000,
                   end_reason="cancelled", turn="turn-c1"),
        ], gen_ms=T0 + 1000, epoch=EPOCH, selected="thread-demo"), tag="cancelled"),
    ])

    # S09 low_battery：needs_you 前置；真实 Power FSM 判定（无画页捷径）。
    # trace：3980→3720→3700（LOW_WARN）→3650→3601→3600 后 1Hz 持续（间隔 ≤2s）：
    # 第一个 ≤3600 有效样本在 6000ms，持续满 30s → 36000ms CRITICAL 强制页。
    b_lines = [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "needs_you",
                   "等待批准运行命令", T0,
                   attn=attention(1, "运行命令需要批准")),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="needs_you"),
        dump_line(1000, "battery_sample", battery(3980, True)),
        dump_line(2000, "battery_sample", battery(3720, True)),
    ]
    tags_at = {3000: "low_warn3700", 20000: "critical_hold", 36000: "forced_page"}
    for i, mv in enumerate([3700, 3650, 3601] + [3600] * 31, start=3):
        at = i * 1000
        ln = dump_line(at, "battery_sample", battery(mv, True),
                       tag=tags_at.get(at))
        b_lines.append(ln)
    # 36000 CRITICAL（锁存；此后无样本，强制页保持）
    b_lines.append(dump_line(36200, "key", {"key": "short_press"},
                             tag="key_rejected"))  # 被拒：无像素变化，不出帧
    b_lines.append(dump_line(36500, "key", {"key": "long_press"},
                             tag="key_long_mute"))
    write_scenario("S09_low_battery.jsonl", [
        "# S09_low_battery — 电池优先于 NEEDS YOU；强制页不可被普通切换覆盖（SCENARIOS §3 S09）",
        "# 电池 trace 满足 §7.2 连续性（临界段 1Hz、间隔 ≤2s）；短按被拒无像素变化 → 无 golden 帧，",
        "# 由 manifest view 断言覆盖（页面仍 low_battery）；长按只静音（MUTED 标签变化）。",
    ], b_lines)

    # S10 battery_unknown：invalid 与范围外两条路径电压均 --（范围外采样与
    # invalid 像素相同 → 不出帧，语义断言覆盖）
    write_scenario("S10_battery_unknown.jsonl", [
        "# S10_battery_unknown — 无效/范围外采样 → 电压 --，不猜百分比（SCENARIOS §3 S10）",
    ], [
        dump_line(0, "app_state", base_working(), tag="online"),
        dump_line(1000, "battery_sample", battery(3900, True), tag="valid_ref"),
        dump_line(2000, "battery_sample", battery(None, False), tag="invalid_sample"),
        dump_line(3000, "battery_sample", battery(5000, True), tag="out_of_range"),
    ])

    # S11 disconnected：150s 无快照自然判定 → 断连提示；恢复取全量并保留页面
    write_scenario("S11_disconnected.jsonl", [
        "# S11_disconnected — 150s 无快照断连（自然计时）+ 全量恢复（SCENARIOS §3 S11）",
        "# 断言：断连提示可见且任务不被改成 IDLE/DONE；恢复后内容保留、计时解冻。",
    ], [
        dump_line(0, "app_state", base_working(), tag="online"),
        dump_line(150000, "advance_time", {"to_ms": 150000}, tag="disconnected"),
        dump_line(150000, "app_state", base_working(seq=2, gen_ms=T0 + 150000),
                  tag="resync"),
        dump_line(151000, "link", {"link_state": "connected"}),
    ])

    # S12 stale：45s 无有效快照 → 陈旧提示独立于业务状态；新 seq 恢复 fresh
    write_scenario("S12_stale.jsonl", [
        "# S12_stale — 45s stale 提示 + 计时冻结 + 恢复（SCENARIOS §3 S12）",
    ], [
        dump_line(0, "app_state", base_working(), tag="fresh"),
        dump_line(45000, "advance_time", {"to_ms": 45000}, tag="stale"),
        dump_line(45000, "app_state", base_working(seq=2, gen_ms=T0 + 45000),
                  tag="recovered"),
    ])

    # S13 multi_agents：8 线程混合六状态 + 总数裁剪标记 + 子页推进 + 选中任务
    threads = [
        thread("t-ny-a", "proj-ny-a", "needs_you", "等待确认部署目标", T0 + 500,
               attn=attention(1, "运行命令需要批准"), turn="turn-a"),
        thread("t-ny-b", "proj-ny-b", "needs_you", "等待批准运行命令", T0 + 500,
               attn=attention(2, "运行命令需要批准"), turn="turn-b"),
        thread("t-err", "proj-err", "error", "迁移数据库失败：编译错误", T0 + 600,
               end_reason="failed", turn="turn-e"),
        thread("t-work-a", "proj-work-a", "working", "正在运行构建与测试", T0 + 400,
               turn="turn-wa"),
        thread("t-work-b", "proj-work-b", "working", "修复登录流程", T0 + 300,
               turn="turn-wb"),
        thread("t-done", "proj-done", "done", "更新文档完成", T0 + 100,
               end_reason="completed", turn="turn-d"),
        thread("t-idle-b", "proj-idle-b", "idle", "等待新任务", T0 + 950,
               turn="turn-ib"),
        thread("t-idle-a", "proj-idle-a", "idle", "等待新任务", T0 + 900,
               turn="turn-ia"),
    ]
    write_scenario("S13_multi_agents.jsonl", [
        "# S13_multi_agents — AGENTS 排序/分页/裁剪 + 选中任务生成其他页（SCENARIOS §3 S13）",
        "# 8 可见 / 共 10（truncated）；排序 needs_you×2(id 升序)→error→working×2→done→idle×2",
    ], [
        dump_line(0, "app_state",
                  state_payload(1, threads, gen_ms=T0, epoch=EPOCH,
                                selected="t-work-a", total=10, truncated=True),
                  tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_p1"),
        dump_line(2000, "key", {"key": "short_press"}, tag="agents_p2"),
        dump_line(3000, "key", {"key": "short_press"}, tag="next_main"),
    ])

    # S14 empty_plan：PLAN 页空态
    write_scenario("S14_empty_plan.jsonl", [
        "# S14_empty_plan — 无计划显示 NO PLAN（暂无计划）、完成数 0（SCENARIOS §3 S14）",
    ], [
        dump_line(0, "app_state", base_working(plan_v=plan(0, [])), tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_pass"),
        dump_line(2000, "key", {"key": "short_press"}, tag="plan_page"),
    ])

    # S15 long_plan：14 步截断、8 可见、128 字节顶格步骤文本、PLAN 分页
    long_step = "检查需求并核对实现界面与运行测试的全部输出"  # 21 CJK = 63B
    long_text = long_step + "-trace-" + long_step + "-payload-ok"  # 144B → 截到 ≤128
    raw = long_text.encode()[:128]
    while raw:  # 回退到完整 UTF-8 码点边界（不切断多字节序列）
        try:
            long_text = raw.decode()
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    assert len(long_text.encode()) in (126, 127, 128)
    steps = [step(f"步骤-{i}", "completed") for i in range(3)]
    steps += [step(long_text, "in_progress")]
    steps += [step("运行测试并整理测试报告", "pending"),
              step("更新文档", "pending"),
              step("编写文档", "pending"),
              step("修复登录", "pending")]
    write_scenario("S15_long_plan.jsonl", [
        "# S15_long_plan — 长计划分页（4/子页）、截断标记、完成数只数 completed、",
        "# 128 字节顶格步骤文本像素截断不覆盖布局（SCENARIOS §3 S15）",
    ], [
        dump_line(0, "app_state", base_working(
            plan_v=plan(14, steps, truncated=True)), tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_pass"),
        dump_line(2000, "key", {"key": "short_press"}, tag="plan_p1"),
        dump_line(3000, "key", {"key": "short_press"}, tag="plan_p2"),
    ])

    # S16 long_project_name：96 字节顶格中英混合项目名（parser cap=96B）
    proj = ""
    for part in ["repos/", "codex-desk-terminal/", "项目目录/", "very-long-name/",
                 "分支/", "branch-main-workspace", "项目目录/"]:
        if len((proj + part).encode()) <= 96:
            proj += part
    proj += "x" * (96 - len(proj.encode()))  # 顶格补齐到 96 字节
    assert len(proj.encode()) == 96
    write_scenario("S16_long_project_name.jsonl", [
        "# S16_long_project_name — 标题栏项目名截断省略、单行、不覆盖状态词（SCENARIOS §3 S16）",
    ], [dump_line(0, "app_state", state_payload(1, [
        thread("thread-demo", proj, "working", "正在运行构建与测试", T0),
    ], gen_ms=T0, epoch=EPOCH, selected="thread-demo"), tag="long_project")])

    # S17 unicode：中英混排 + 全角标点 + 1 emoji（字体无字形 → 可见 '?'）
    write_scenario("S17_unicode.jsonl", [
        "# S17_unicode — 中文/英文/全角标点混排 + emoji 可见替代符（SCENARIOS §3 S17）",
        "# emoji U+1F680 刻意不在字体子集：ascii_safe 折为 '?'，不静默缺字、不崩溃。",
    ], [dump_line(0, "app_state", state_payload(1, [
        thread("thread-demo", "中文项目（Mixed）目录", "working",
               "正在运行构建与测试，检查需求 🚀 并更新文档", T0,
               attn=attention(1, "运行命令需要批准（紧急）")),
    ], gen_ms=T0, epoch=EPOCH, selected="thread-demo",
        usage_v=usage([window("quota-cn", "窗口 A（本周）", 42.0, 300,
                              T0 + 3600000)])), tag="unicode")])

    # S18 usage_missing：额度缺失 --；context --；token 不冒充 context
    write_scenario("S18_usage_missing.jsonl", [
        "# S18_usage_missing — 额度缺失 USAGE -- / CTX --（SCENARIOS §3 S18）",
    ], [
        dump_line(0, "app_state", state_payload(1, [
            thread("thread-demo", "codex-desk-terminal", "working",
                   "正在运行构建与测试", T0,
                   ctx=context(used=123000)),
        ], gen_ms=T0, epoch=EPOCH, selected="thread-demo",
            usage_v=usage([], available=False)), tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_pass"),
        dump_line(2000, "key", {"key": "short_press"}, tag="plan_pass"),
        dump_line(3000, "key", {"key": "short_press"}, tag="usage_page"),
    ])

    # S19 usage_0：0% 合法显示；窗口名/长度来自数据；reset 倒计时按数据计算
    write_scenario("S19_usage_0.jsonl", [
        "# S19_usage_0 — 0% + 实际窗口长度 + reset 倒计时（T0+4h → RST 04:00:00）",
    ], [
        dump_line(0, "app_state", base_working(usage_v=usage([
            window("codex-primary", "300 MIN WINDOW", 0.0, 300, T0 + 4 * 3600000)])),
            tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_pass"),
        dump_line(2000, "key", {"key": "short_press"}, tag="plan_pass"),
        dump_line(3000, "key", {"key": "short_press"}, tag="usage_page"),
    ])

    # S20 usage_100：100% 不溢出、进度满格
    write_scenario("S20_usage_100.jsonl", [
        "# S20_usage_100 — 100% 顶格显示不溢出（SCENARIOS §3 S20）",
    ], [
        dump_line(0, "app_state", base_working(usage_v=usage([
            window("codex-primary", "300 MIN WINDOW", 100.0, 300, T0 + 3600000)])),
            tag="online"),
        dump_line(1000, "key", {"key": "short_press"}, tag="agents_pass"),
        dump_line(2000, "key", {"key": "short_press"}, tag="plan_pass"),
        dump_line(3000, "key", {"key": "short_press"}, tag="usage_page"),
    ])

    # S21 bridge_restart：epoch A(seq=5) → 断连 → epoch B(seq=0 同内容) → 恢复
    write_scenario("S21_bridge_restart.jsonl", [
        "# S21_bridge_restart — Bridge 重启（新 epoch seq=0）全量快照被接受，",
        "# UI 不回退空白/IDLE，状态收敛同一业务内容（SCENARIOS §3 S21；对齐 P1.2 replay 语义）",
    ], [
        dump_line(0, "app_state", base_working(seq=5, epoch="epoch-a-001"),
                  tag="epoch_a"),
        dump_line(1000, "link", {"link_state": "disconnected"}, tag="restarting"),
        dump_line(2000, "app_state", base_working(seq=0, gen_ms=T0 + 2000,
                                                  epoch="epoch-b-002"),
                  tag="epoch_b"),
        dump_line(3000, "link", {"link_state": "connected"}, tag="resync"),
    ])


# ------------------------------------------------------- 正式 S_lifecycle（P2.5）

def lifecycle_snapshots() -> list[dict]:
    """进程内跑 P1.2 replay 源（bridge/sources/replay.py），返回快照列表。
    与命令行等价：python -m bridge --source replay \\
      --file tests/fixtures/bridge/lifecycle_events.jsonl \\
      --epoch lifecycle-001 --anchor-ms 1789000000000"""
    from bridge.sources import replay as bridge_replay
    events = bridge_replay.load_events(
        str(REPO / "tests/fixtures/bridge/lifecycle_events.jsonl"))
    return bridge_replay.run_events(events, epoch="lifecycle-001",
                                    utc_anchor_ms=T0)


LIFECYCLE_HEADER = [
    "# S_lifecycle.jsonl — P2.5 全生命周期回放正式 fixture（A6 定稿，2026-09-10）",
    "#",
    "# 业务快照来源（对齐声明，真源 tests/SCENARIOS.md §3.1）：",
    "#   全部 app_state payload = bridge replay 源对 P1.2 lifecycle 事件序列的快照原文：",
    "#     uv run --python 3.12 python -m bridge --source replay \\",
    "#       --file tests/fixtures/bridge/lifecycle_events.jsonl \\",
    "#       --epoch lifecycle-001 --anchor-ms 1789000000000",
    "#   （快照 seq N 的虚拟 at_ms = generated_at_ms - T0；由 scripts/gen_scenarios.py 生成）",
    "#",
    "# 电池 trace 独立驱动 LOW BATTERY（INTERFACES §4：不得画捷径页）：",
    "#   满足 §7.2 连续性：有效样本间隔 ≤2s；3600ms 起 ≤3600mV 1Hz 持续 30s，",
    "#   由 shared/power（P5.1 纯 FSM，与固件同源）判定 CRITICAL → 强制页。",
    "#",
    "# checkpoint 帧清单（tag → 期望页面/状态词；逐帧断言规则 UI_CONTRACT §4，不只验末帧）：",
    "#   idle          → NOW 页，状态词 IDLE（P1.2 thread_started 起始快照）",
    "#   working       → NOW 页，状态词 WORKING（turn_started）",
    "#   plan_update   → NOW 页，状态词仍 WORKING，PLAN 摘要更新为 1/3（内容事件不加状态）",
    "#   needs_you     → NOW 页，状态词 NEEDS YOU，反白粗框，等待提醒可见",
    "#   done          → NOW 页，状态词 DONE，粗框消失，pending 清零，时长定格",
    "#   low_battery   → LOW BATTERY 强制页（电压、0%、低压提示），优先于业务页",
    "#   key_rejected  → 短按被拒（无像素变化，不出帧；由 manifest view 断言覆盖）",
    "#   key_long_mute → 强制页保持 + MUTED 标签（长按不解除强制页）；生命周期至此",
    "#   （critical 锁存不因电池反弹撤销；断连/陈旧/恢复语义由 S11/S12，重同步由 S21）",
]


def build_lifecycle() -> None:
    snaps = lifecycle_snapshots()
    lines: list[str] = []
    # 业务快照：at = generated_at_ms - T0（anchor 恒等映射回事件 at_ms）
    event_tags = {0: "idle", 100: "working", 300: "plan_update",
                  900: "needs_you", 2100: "done"}
    for snap in snaps:
        at = snap["generated_at_ms"] - T0
        lines.append(dump_line(at, "app_state", snap,
                               tag=event_tags.get(at)))
    # 电池 trace：t=2500 起，间隔 500ms 降至临界，再 1Hz 持续 30s（无捷径）
    seq_at = [(2500, 3980), (3000, 3860), (3500, 3720), (4000, 3700),
              (4500, 3650), (5000, 3601)]
    seq_at += [(5500 + i * 1000, 3600) for i in range(31)]  # 5500..35500
    for i, (at, mv) in enumerate(seq_at):
        tag = "low_battery" if (mv <= 3600 and at == 35500) else None
        lines.append(dump_line(at, "battery_sample", battery(mv, True), tag=tag))
    # 3600@5500 起 ≤3600 连续至 35500 = 30s → CRITICAL 强制页（tag low_battery）
    # 35500 CRITICAL（锁存；此后无样本，强制页保持）；
    # 按键在强制页上：短按被拒（无帧）、长按只静音
    lines.append(dump_line(35750, "key", {"key": "short_press"},
                           tag="key_rejected"))  # 被拒：不出帧
    lines.append(dump_line(36000, "key", {"key": "long_press"},
                           tag="key_long_mute"))
    # 低压后critical 锁存不因反弹撤销，最终低压页为生命周期终点（P2.5 验收）；
    # 断连/陈旧/恢复语义由 S11/S12 覆盖，重同步由 S21 覆盖。
    write_scenario("S_lifecycle.jsonl", LIFECYCLE_HEADER, lines)


COVERED = set(collect_codepoints())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="重新生成并与磁盘逐字节比对（不写入）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    existing = {p.name: p.read_text(encoding="utf-8") for p in OUT.glob("*.jsonl")}

    # 先生成到内存（write_scenario 直接写盘，故 --check 用目录快照对比）
    build_all()
    build_lifecycle()

    if args.check:
        ok = True
        for p in sorted(OUT.glob("*.jsonl")):
            if existing.get(p.name) != p.read_text(encoding="utf-8"):
                ok = False
                print(f"差异：{p}")
        print("check: 一致" if ok else "check: 不一致（需重新生成）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
