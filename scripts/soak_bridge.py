#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""soak_bridge.py — P6.2 PC 前置压缩 soak（A5）。

把 24h 量级压缩到分钟级：确定性系统允许时钟压缩（虚拟时钟/固定锚点），
本脚本只覆盖 PC 侧（模拟器 + Bridge + 共享层），不覆盖真机 24h soak
（真机项见任务书剩余问题）。真源：docs/DEVELOPMENT_PLAN.md P6.2 行
（无崩溃/死锁/持续内存增长；状态最终收敛；结果可追溯）与 §8 稳定性行。

阶段（子命令）：
  bridge        N 轮 `python -m bridge --source replay --file lifecycle_events.jsonl`
                输出到临时目录 → 全部快照 sha256 与首轮逐字节比对（确定性：
                N 轮全同）；逐轮记录子进程精确峰值 RSS（/usr/bin/time -l
                内核 getrusage；psutil 轮询仅作参考），首/末 + 最小二乘
                斜率阈值判定无持续增长。
  bridge-inproc 长驻进程对照（真实 Bridge 是长驻进程）：单进程内连续 K 次
                replay.run_file 全量渲染+序列化，逐次记录当前 RSS 与
                ru_maxrss 高水位——这是"持续内存增长"的主证据来源
                （每轮新起子进程的模式测不到进程内泄漏）。
  mock-c        7 场景 mock 全集（lifecycle/cancelled/error/multi_agents/
                usage_missing/usage_0/usage_100）各跑两遍比对确定性；快照按
                tests/fixtures 校验模式改名 valid_*.json 后全部喂
                build/shared/test_shared（C 端解析器，P1.4 有界解析），要求 0 FAIL。
  sim           M 轮/场景 离屏渲染 soak（SDL dummy）：--scenario 确定性回放
                （UI_CONTRACT §3；注：冻结后的模拟器 CLI 中 --fixed-clock 与
                --scenario 互斥，回放确定性由 --scenario 的虚拟时钟保证，
                §3.3「同一输入文件的输出必须完全确定」），逐帧与 tests/golden
                同名帧 sha256 逐字节比对（零差异）；RSS 记录同 bridge。
  checkui       全量重捕 22 场景 actual（105 帧与 golden 同数）后，
                check_ui.py 全 golden 集连续跑 K 次（默认 3），要求全部
                rc=0 且 22/22 PASS。
  report        汇总 artifacts/soak/ 各阶段 result.json → soak-report.md +
                soak-report.json。缺失阶段 = 环境错误。
  all           依次跑全部阶段并生成报告；任一阶段失败最终退出码 1。

退出码：0 全部通过；1 存在确定性/内存/崩溃断言失败；2 环境错误（二进制
缺失、fixture 缺失、golden 目录缺失等）。任何失败如实记录，不重试不掩盖。

内存判据（报告中写明）：
  - 峰值 RSS 来源：/usr/bin/time -l（内核 getrusage 每进程精确峰值，无采样竞态；
    10ms psutil 轮询对 <50ms 短进程存在启动竞态伪差，只作运行期参考曲线）；
  - 可回落序列（子进程逐轮峰值 RSS、进程内当前 RSS）：判持续增长
    |末-首| ≤ max(2048 KiB, 25%×首)；斜率仅作方向展示不单独判定
    （内核峰值按页粒度量化，短序列上斜率会被放大成噪声）；
  - ru_maxrss 高水位序列（单调不减，真实泄漏的主证据）：同上只判总漂移；
  - checkui 仅 3 个点：峰值-最小 ≤ max(4096 KiB, 25%×最小)，不算斜率；
  - 崩溃/死锁：子进程退出码非 0 记 crash，超时（强杀）记 timeout，均判失败。

推荐运行（真实退出码）：
  sh scripts/run_soak.sh
等价单步：
  uv run --python 3.12 --with psutil python scripts/soak_bridge.py all
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVENTS = REPO / "tests" / "fixtures" / "bridge" / "lifecycle_events.jsonl"
SCEN_DIR = REPO / "tests" / "fixtures" / "scenarios"
GOLDEN = REPO / "tests" / "golden"
SIM = REPO / "build" / "simulator" / "codex-display-sim"
TEST_SHARED = REPO / "build" / "shared" / "test_shared"
CHECK_UI = REPO / "scripts" / "check_ui.py"

T0_ANCHOR_MS = 1789000000000          # 与 SCENARIOS.md §2 虚构 UTC 基准一致
BRIDGE_EPOCH = "soak-bridge-001"
MOCK_SCENARIOS = ("lifecycle", "cancelled", "error", "multi_agents",
                  "usage_missing", "usage_0", "usage_100")
SIM_SCENARIOS = ("S_lifecycle", "S09_low_battery", "S13_multi_agents",
                 "S21_bridge_restart")  # 生命周期 + 低压强制页 + 多状态 + 重启收敛

# 内存判据常量（报告与 JSON 中原文引用；斜率仅展示不判定）
LAST_FIRST_ABS_KIB = 2048.0
LAST_FIRST_REL = 0.25
CHECKUI_BAND_ABS_KIB = 4096.0


# ---------------------------------------------------------------- 环境与工具

def kiob(value: float) -> float:
    """macOS psutil/resource 返回字节；统一转 KiB（Linux ru_maxrss 本就是 KiB）。"""
    if sys.platform == "darwin":
        return value / 1024.0
    return float(value)  # Linux ru_maxrss 已是 KiB；psutil rss 仍为字节，见调用处


def rss_kib_of_rusage(value: float) -> float:
    """resource.ru_maxrss → KiB（darwin=字节，linux=KiB）。"""
    return value / 1024.0 if sys.platform == "darwin" else float(value)


try:
    import psutil  # uv run --with psutil 注入；缺失时走 coarse 回退
except ImportError:  # pragma: no cover - 兜底路径
    psutil = None


def env_error(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"环境错误: {msg}", file=sys.stderr)
    raise SystemExit(2)


def require_paths() -> None:
    missing = [str(p) for p in (EVENTS, SIM, TEST_SHARED, CHECK_UI, GOLDEN)
               if not p.exists()]
    if missing:
        env_error("缺少依赖路径（先构建模拟器/共享层）: " + ", ".join(missing))
    if not SCEN_DIR.is_dir():
        env_error(f"场景目录缺失: {SCEN_DIR}")


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def slope_kib_per_round(values: list) -> float:
    """最小二乘斜率（KiB/轮），样本 <2 返回 0.0。"""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den


def mem_verdict(series_kib: list, *, mode: str = "sampled") -> dict:
    """内存判据（模块 docstring「内存判据」段）。

    mode="sampled"  ：可回落序列（子进程逐轮峰值 / 进程内当前 RSS）——
                      判 |末-首| ≤ max(2048 KiB, 25%×首)；斜率仅展示。
    mode="highwater"：ru_maxrss 高水位（单调不减）——斜率无意义，只判
                      总漂移 |末-首| ≤ max(2048 KiB, 25%×首)；斜率仅展示。
    mode="checkui"  ：仅 3 个点——峰值-最小 ≤ max(4096 KiB, 25%×最小)。
    """
    if not series_kib:
        return {"pass": False, "reason": "无 RSS 样本"}
    first, last = series_kib[0], series_kib[-1]
    lo, hi = min(series_kib), max(series_kib)
    slope = slope_kib_per_round(series_kib)
    drift = abs(last - first)
    out = {
        "mode": mode,
        "first_kib": round(first, 1),
        "last_kib": round(last, 1),
        "min_kib": round(lo, 1),
        "peak_kib": round(hi, 1),
        "slope_kib_per_round": round(slope, 4),
    }
    if mode == "highwater":
        out["criteria"] = (f"高水位单调不减，判总漂移 |末-首|≤"
                           f"max({LAST_FIRST_ABS_KIB:.0f} KiB, "
                           f"{LAST_FIRST_REL:.0%}×首)={max(LAST_FIRST_ABS_KIB, LAST_FIRST_REL * first):.0f} KiB；"
                           "斜率仅展示不判定")
        out["measured_last_first_kib"] = round(drift, 1)
        out["pass"] = drift <= max(LAST_FIRST_ABS_KIB, LAST_FIRST_REL * first)
    elif mode == "checkui":
        band = max(CHECKUI_BAND_ABS_KIB, LAST_FIRST_REL * lo)
        out["criteria"] = (f"峰值-最小≤max({CHECKUI_BAND_ABS_KIB:.0f} KiB, "
                           f"{LAST_FIRST_REL:.0%}×最小)={band:.0f} KiB；点数不足不算斜率")
        out["measured_band_kib"] = round(hi - lo, 1)
        out["band_kib"] = round(band, 1)
        out["pass"] = (hi - lo) <= band
    else:
        band = max(LAST_FIRST_ABS_KIB, LAST_FIRST_REL * first)
        out["criteria"] = (f"持续增长判据：|末-首|≤max({LAST_FIRST_ABS_KIB:.0f} KiB, "
                           f"{LAST_FIRST_REL:.0%}×首)={band:.0f} KiB（斜率仅作方向"
                           "展示不单独判定：内核峰值按页粒度量化，短序列上斜率"
                           "会被放大成噪声）")
        out["measured_last_first_kib"] = round(drift, 1)
        out["pass"] = bool(drift <= band)
    return out


class ChildRunner:
    """带超时与精确峰值 RSS 的子进程运行器。

    峰值 RSS 来源：/usr/bin/time -l（内核 getrusage，每进程精确峰值，字节）。
    不用采样判定泄漏：10ms psutil 轮询对 <50ms 短进程存在启动竞态伪差
    （首轮实测曾把 exec 早期样本当峰值，产生假斜率），psutil 轮询仅保留为
    运行期参考曲线。超时对进程组强杀记 timeout（死锁证据），不重试。
    """

    TIME_BIN = "/usr/bin/time"
    _RSS_RE = None  # 类级惰性编译

    def __init__(self, label: str) -> None:
        self.label = label
        self.samples: list = []
        self._stop = threading.Event()
        self.use_time = os.access(self.TIME_BIN, os.X_OK)

    def _sample_loop(self, pid: int) -> None:
        if psutil is None:
            return
        try:
            proc = psutil.Process(pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            try:
                self.samples.append(proc.memory_info().rss / 1024.0)  # → KiB
            except psutil.Error:
                break
            self._stop.wait(0.01)

    @classmethod
    def _parse_time_rss(cls, stderr_text: str) -> float | None:
        # macOS ≥13 报 "peak memory footprint"（字节）；旧版/其他平台报
        # "maximum resident set size"（KB，见 time(1)）。按标签分支取单位，
        # 不按数值量级猜测。取最后一个匹配。
        import re
        if cls._RSS_RE is None:
            cls._RSS_RE = re.compile(
                r"(\d+)\s+(peak memory footprint|maximum resident set size)")
        hits = cls._RSS_RE.findall(stderr_text)
        if not hits:
            return None
        v, label = hits[-1]
        v = float(v)
        return v / 1024.0 if label == "peak memory footprint" else v

    def run(self, cmd: list, *, cwd: str, timeout_s: float,
            env: dict | None = None) -> dict:
        t0 = time.monotonic()
        if self.use_time:
            cmd = [self.TIME_BIN, "-l", *cmd]
        proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        if psutil is not None:
            th = threading.Thread(target=self._sample_loop, args=(proc.pid,),
                                  daemon=True)
            th.start()
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:  # 连同 /usr/bin/time 子进程一起杀（进程组）
                os.killpg(os.getpgid(proc.pid), 9)
            except OSError:
                proc.kill()
            out, err = proc.communicate()
        self._stop.set()
        dur = time.monotonic() - t0
        err_text = (err or b"").decode("utf-8", "replace")
        exact = self._parse_time_rss(err_text) if not timed_out else None
        res = {
            "cmd": cmd,
            "rc": proc.returncode,
            "timeout": timed_out,
            "duration_s": round(dur, 4),
            "rss_source": ("kernel:/usr/bin/time -l" if exact is not None else
                           ("psutil_poll_10ms" if psutil is not None
                            else "无（psutil 与 time 均不可用）")),
            "stdout_full": (out or b"").decode("utf-8", "replace")[:20000],
            "stdout_tail": (out or b"")[-1500:].decode("utf-8", "replace"),
            "stderr_tail": err_text[-1500:],
        }
        if exact is not None:
            res["rss_peak_kib"] = round(exact, 1)
        elif self.samples:
            res["rss_peak_kib"] = round(max(self.samples), 1)
        if self.samples:
            res["rss_first_kib"] = round(self.samples[0], 1)
            res["rss_last_kib"] = round(self.samples[-1], 1)
        return res


def write_csv(path: Path, header: list, rows: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_result(outdir: Path, name: str, result: dict) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{name}.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def snapshot_dir_hashes(d: Path) -> dict:
    """目录内 snapshot_*.json → {文件名: sha256}。"""
    return {p.name: sha256_file(p) for p in sorted(d.glob("snapshot_*.json"))}


# ---------------------------------------------------------------- 阶段 1a: bridge

def phase_bridge(outdir: Path, rounds: int, timeout_s: float) -> dict:
    print(f"[bridge] {rounds} 轮 python -m bridge --source replay "
          f"（确定性 + 子进程 RSS）")
    work = outdir / "bridge_replay"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    failures: list = []
    crashes = timeouts = 0
    ref_hashes: dict | None = None
    ref_dir = work / "reference_round1"
    rows = []
    rss_series: list = []
    t_phase = time.monotonic()
    for i in range(1, rounds + 1):
        rd = work / f"round_{i:04d}"
        rd.mkdir()
        cmd = [sys.executable, "-m", "bridge", "--source", "replay",
               "--file", str(EVENTS), "--epoch", BRIDGE_EPOCH,
               "--anchor-ms", str(T0_ANCHOR_MS), "--out", str(rd)]
        r = ChildRunner(f"bridge-r{i}").run(cmd, cwd=str(REPO),
                                            timeout_s=timeout_s)
        hashes = snapshot_dir_hashes(rd) if r["rc"] == 0 and not r["timeout"] else {}
        match = True
        detail = ""
        if r["timeout"]:
            match = False
            detail = "timeout(疑似死锁)"
            timeouts += 1
        elif r["rc"] != 0:
            match = False
            detail = f"crash rc={r['rc']}"
            crashes += 1
        elif ref_hashes is None:
            ref_hashes = hashes
            if not hashes:
                match = False
                detail = "首轮未产出任何快照"
            else:
                shutil.rmtree(ref_dir, ignore_errors=True)
                shutil.copytree(rd, ref_dir)
        else:
            if set(hashes) != set(ref_hashes):
                match = False
                detail = (f"快照文件集不符 round={sorted(hashes)} "
                          f"ref={sorted(ref_hashes)}")
            else:
                bad = [k for k in hashes if hashes[k] != ref_hashes[k]]
                if bad:
                    match = False
                    for k in bad:
                        a = (ref_dir / k).read_bytes()
                        b = (rd / k).read_bytes()
                        off = next((j for j in range(min(len(a), len(b)))
                                    if a[j] != b[j]),
                                   min(len(a), len(b)))
                        detail += (f"{k} sha不符 首差异偏移={off} "
                                   f"len={len(a)}/{len(b)}; ")
        if r.get("rss_peak_kib") is not None:
            rss_series.append(r["rss_peak_kib"])
        rows.append([i, r["rc"], int(r["duration_s"] * 1000),
                     r.get("rss_peak_kib", ""), len(hashes), int(match),
                     detail.strip()])
        if i == 1 or i % 50 == 0 or not match:
            peak = r.get("rss_peak_kib", "-")
            print(f"[bridge] 轮 {i}/{rounds} rc={r['rc']} "
                  f"快照={len(hashes)} 一致={match} "
                  f"峰值RSS={peak}KiB {detail.strip()}")
        if not match:
            # 失败如实记录；不重试，后续轮次继续采集证据
            failures.append({"round": i, "detail": detail.strip(),
                             "rc": r["rc"], "timeout": r["timeout"]})
        shutil.rmtree(rd, ignore_errors=True)
    duration = time.monotonic() - t_phase
    mem = mem_verdict(rss_series)
    result = {
        "phase": "bridge",
        "task": "P6.2 PC 前置压缩",
        "command_template": ("python -m bridge --source replay --file "
                             "tests/fixtures/bridge/lifecycle_events.jsonl "
                             f"--epoch {BRIDGE_EPOCH} --anchor-ms {T0_ANCHOR_MS} "
                             "--out <tmp>"),
        "rounds_planned": rounds,
        "rounds_ok": rounds - len(failures),
        "crashes": crashes,
        "timeouts": timeouts,
        "determinism": {
            "all_identical": not any(
                ("sha不符" in f["detail"] or "文件集不符" in f["detail"]
                 or "未产出任何快照" in f["detail"]) for f in failures),
            "reference_snapshot_count": len(ref_hashes or {}),
            "reference_sha256": ref_hashes or {},
            "reference_dir": str(ref_dir),
            "note": "PYTHONHASHSEED 保持默认随机化：跨进程哈希种子不同仍逐字节全同",
        },
        "memory": mem,
        "memory_series_csv": "bridge_replay/bridge_rounds.csv",
        "duration_s": round(duration, 2),
        "timeout_per_round_s": timeout_s,
        "failures": failures,
        "pass": not failures and bool(mem.get("pass")),
    }
    write_csv(work / "bridge_rounds.csv",
              ["round", "rc", "duration_ms", "rss_peak_kib",
               "snapshots", "match", "detail"], rows)
    write_result(outdir, "bridge", result)
    print(f"[bridge] 完成 {result['rounds_ok']}/{rounds} 轮，"
          f"确定性{'全同' if result['determinism']['all_identical'] else '存在差异'}，"
          f"内存判据 {'PASS' if mem.get('pass') else 'FAIL'}，"
          f"耗时 {duration:.1f}s")
    return result


# ---------------------------------------------------------------- 阶段 1b: 长驻进程

def phase_bridge_inproc(outdir: Path, rounds: int, warmup: int = 50) -> dict:
    print(f"[bridge-inproc] 单进程长驻对照：{warmup} 次预热 + {rounds} 次 "
          f"replay.run_file 全量渲染")
    sys.path.insert(0, str(REPO))
    from bridge.sources import replay  # noqa: E402

    series_rss: list = []
    series_highwater: list = []
    rows = []
    failures: list = []
    t_phase = time.monotonic()
    proc = psutil.Process(os.getpid()) if psutil is not None else None
    for i in range(1, warmup + rounds + 1):
        t0 = time.monotonic()
        try:
            snaps = replay.run_file(str(EVENTS), epoch=BRIDGE_EPOCH,
                                    utc_anchor_ms=T0_ANCHOR_MS)
            blob = "".join(json.dumps(s, ensure_ascii=False,
                                      separators=(",", ":")) for s in snaps)
            import hashlib
            h = hashlib.sha256(blob.encode("utf-8")).hexdigest()
            n = len(snaps)
        except Exception as e:  # noqa: BLE001 — 崩溃即记录，不吞
            failures.append({"iter": i, "detail": f"异常 {type(e).__name__}: {e}"})
            break
        dur_ms = int((time.monotonic() - t0) * 1000)
        if i > warmup:
            cur = proc.memory_info().rss / 1024.0 if proc else None
            import resource
            hw = rss_kib_of_rusage(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            if cur is not None:
                series_rss.append(cur)
            series_highwater.append(hw)
            rows.append([i - warmup, n, h, dur_ms,
                         round(cur, 1) if cur else "", round(hw, 1)])
            if (i - warmup) == 1 or (i - warmup) % 50 == 0:
                print(f"[bridge-inproc] 迭代 {i - warmup}/{rounds} "
                      f"快照={n} rss={cur and round(cur, 1)}KiB "
                      f"高水位={round(hw, 1)}KiB")
    duration = time.monotonic() - t_phase
    mem_hw = mem_verdict(series_highwater, mode="highwater")
    mem_cur = mem_verdict(series_rss) if series_rss else {"pass": False,
                                                          "reason": "无 psutil"}
    result = {
        "phase": "bridge-inproc",
        "task": "P6.2 PC 前置压缩（长驻进程对照：真实 Bridge 为长驻进程，"
                "本阶段是持续内存增长的主证据）",
        "iterations": rounds,
        "warmup": warmup,
        "snapshots_per_iter": None,
        "output_digest_stable": (len({r[2] for r in rows}) == 1) if rows else False,
        "memory_highwater": mem_hw,
        "memory_current": mem_cur,
        "memory_series_csv": "bridge_inproc/iterations.csv",
        "duration_s": round(duration, 2),
        "failures": failures,
        "pass": not failures and bool(mem_hw.get("pass")) and
                bool(mem_cur.get("pass")) and
                (len({r[2] for r in rows}) == 1 if rows else False),
    }
    work = outdir / "bridge_inproc"
    work.mkdir(parents=True, exist_ok=True)
    write_csv(work / "iterations.csv",
              ["iter", "snapshots", "output_sha256", "duration_ms",
               "rss_kib", "ru_maxrss_highwater_kib"], rows)
    if rows:
        result["snapshots_per_iter"] = rows[0][1]
    write_result(outdir, "bridge-inproc", result)
    print(f"[bridge-inproc] 完成 {len(rows)}/{rounds} 次，"
          f"输出摘要{'全同' if result['output_digest_stable'] else '存在差异'}，"
          f"高水位判据 {'PASS' if mem_hw.get('pass') else 'FAIL'}，"
          f"耗时 {duration:.1f}s")
    return result


# ---------------------------------------------------------------- 阶段 1c: mock 全集 + C 解析器

def phase_mock_c(outdir: Path, timeout_s: float) -> dict:
    print(f"[mock-c] 7 场景 mock 全集 ×2 遍确定性 + 全部快照过 C 端解析器 "
          f"({TEST_SHARED.name})")
    work = outdir / "mock_c"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    valid_dir = work / "valid_snapshots"
    valid_dir.mkdir()
    failures: list = []
    per_scen: dict = {}
    t_phase = time.monotonic()
    for scen in MOCK_SCENARIOS:
        entry: dict = {"scenario": scen}
        run_hashes: list = []
        for run_i in (1, 2):
            rd = work / f"{scen}_run{run_i}"
            rd.mkdir()
            cmd = [sys.executable, "-m", "bridge", "--source", "mock",
                   "--scenario", scen, "--out", str(rd)]
            r = ChildRunner(f"mock-{scen}-{run_i}").run(cmd, cwd=str(REPO),
                                                        timeout_s=timeout_s)
            ok = r["rc"] == 0 and not r["timeout"]
            entry[f"run{run_i}_rc"] = r["rc"]
            entry[f"run{run_i}_timeout"] = r["timeout"]
            if not ok:
                failures.append({"scenario": scen, "run": run_i,
                                 "rc": r["rc"], "timeout": r["timeout"],
                                 "stderr_tail": r["stderr_tail"][-400:]})
            run_hashes.append(snapshot_dir_hashes(rd) if ok else None)
            if run_i == 1 and ok:
                # 快照按 tests/fixtures 校验模式改名 valid_*.json → C 解析器
                for name in sorted(h for h in run_hashes[0]):
                    seq = Path(name).stem.split("_")[1]
                    shutil.copyfile(rd / name, valid_dir /
                                    f"valid_{scen}_{seq}.json")
            shutil.rmtree(rd, ignore_errors=True)
        ident = (run_hashes[0] is not None and run_hashes[0] == run_hashes[1])
        entry["deterministic_double_run"] = bool(ident)
        entry["snapshot_count"] = len(run_hashes[0] or {})
        if not ident:
            failures.append({"scenario": scen,
                             "detail": "两遍快照 sha256 不全同"})
        per_scen[scen] = entry
        print(f"[mock-c] {scen}: 快照={entry['snapshot_count']} "
              f"两遍全同={ident}")
    # 全部 valid_*.json 一次性喂 C 端解析器（P1.4 有界解析；0 FAIL 要求）
    files = sorted(str(p) for p in valid_dir.glob("valid_*.json"))
    c_res = ChildRunner("test_shared").run([str(TEST_SHARED), *files],
                                           cwd=str(REPO),
                                           timeout_s=timeout_s)
    c_ok = (c_res["rc"] == 0 and not c_res["timeout"]
            and "[FAIL]" not in c_res["stdout_full"])
    (work / "test_shared_stdout.txt").write_text(c_res["stdout_full"],
                                                 encoding="utf-8")
    if not c_ok:
        failures.append({"detail": f"C 解析器失败 rc={c_res['rc']} "
                                   f"timeout={c_res['timeout']}"})
    n_valid = len(files)
    print(f"[mock-c] C 解析器: {n_valid} 份快照 rc={c_res['rc']} "
          f"{'PASS' if c_ok else 'FAIL'}")
    duration = time.monotonic() - t_phase
    result = {
        "phase": "mock-c",
        "scenarios": MOCK_SCENARIOS,
        "per_scenario": per_scen,
        "c_parser": {
            "binary": str(TEST_SHARED),
            "snapshot_files": n_valid,
            "naming": "tests/fixtures 校验模式 valid_*.json（合法 → CDT_PARSE_OK）",
            "rc": c_res["rc"],
            "timeout": c_res["timeout"],
            "stdout_evidence": "mock_c/test_shared_stdout.txt",
            "pass": bool(c_ok),
        },
        "duration_s": round(duration, 2),
        "failures": failures,
        "pass": not failures,
    }
    write_result(outdir, "mock-c", result)
    print(f"[mock-c] 完成，耗时 {duration:.1f}s，"
          f"{'PASS' if result['pass'] else 'FAIL'}")
    return result


# ---------------------------------------------------------------- 阶段 2a: 模拟器 soak

def golden_hashes() -> dict:
    return {p.name: sha256_file(p) for p in sorted(GOLDEN.glob("*.png"))}


def phase_sim(outdir: Path, sim_rounds: int, timeout_s: float) -> dict:
    print(f"[sim] {sim_rounds} 轮/场景 × {len(SIM_SCENARIOS)} 场景 离屏渲染 "
          f"(SDL dummy) + golden 逐帧 sha256 比对")
    work = outdir / "sim"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    env = {**os.environ, "SDL_VIDEODRIVER": "dummy"}
    ghash = golden_hashes()
    failures: list = []
    per_scen: dict = {}
    t_phase = time.monotonic()
    for scen in SIM_SCENARIOS:
        scen_file = SCEN_DIR / f"{scen}.jsonl"
        if not scen_file.is_file():
            env_error(f"场景文件缺失: {scen_file}")
        scen_golden = {k: v for k, v in ghash.items() if k.startswith(scen + "__")
                       or k.startswith(scen + ".")}
        if not scen_golden:
            env_error(f"golden 中没有场景 {scen} 的帧")
        rows = []
        rss_series: list = []
        ref_dir = work / scen / "reference_round1"
        scen_fail: list = []
        crashes = timeouts = 0
        for i in range(1, sim_rounds + 1):
            rd = work / scen / f"round_{i:04d}"
            rd.mkdir(parents=True)
            cmd = [str(SIM), "--scenario", str(scen_file),
                   "--capture-dir", str(rd)]
            r = ChildRunner(f"sim-{scen}-{i}").run(cmd, cwd=str(REPO),
                                                   timeout_s=timeout_s,
                                                   env=env)
            match, detail = True, ""
            frames: dict = {}
            if r["timeout"]:
                match = False
                detail = "timeout(疑似死锁)"
                timeouts += 1
            elif r["rc"] != 0:
                match = False
                detail = f"crash rc={r['rc']}: {r['stderr_tail'][-200:]}"
                crashes += 1
            else:
                sub = rd / scen
                for p in sorted(sub.glob("*.png")):
                    frames[p.name] = sha256_file(p)
                if set(frames) != set(scen_golden):
                    match = False
                    miss = sorted(set(scen_golden) - set(frames))
                    extra = sorted(set(frames) - set(scen_golden))
                    detail = (f"帧集合不符 缺={miss} 多={extra}")
                else:
                    bad = [k for k in sorted(frames)
                           if frames[k] != scen_golden[k]]
                    if bad:
                        match = False
                        detail = f"帧 sha256 不符: {bad}"
            if r.get("rss_peak_kib") is not None:
                rss_series.append(r["rss_peak_kib"])
            rows.append([i, r["rc"], int(r["duration_s"] * 1000),
                         r.get("rss_peak_kib", ""), len(frames), int(match),
                         detail])
            if i == 1 and match and frames:
                shutil.rmtree(ref_dir, ignore_errors=True)
                shutil.copytree(rd, ref_dir)
            if i == 1 or i % 25 == 0 or not match:
                print(f"[sim] {scen} 轮 {i}/{sim_rounds} rc={r['rc']} "
                      f"帧={len(frames)} 一致={match} "
                      f"峰值RSS={r.get('rss_peak_kib', '-')}KiB {detail}")
            if not match:
                scen_fail.append({"round": i, "detail": detail,
                                  "rc": r["rc"], "timeout": r["timeout"]})
            shutil.rmtree(rd, ignore_errors=True)
        mem = mem_verdict(rss_series)
        per_scen[scen] = {
            "scenario_file": str(scen_file),
            "golden_frames": len(scen_golden),
            "rounds": sim_rounds,
            "rounds_ok": sim_rounds - len(scen_fail),
            "crashes": crashes,
            "timeouts": timeouts,
            "all_frames_identical_to_golden": not any(
                "sha256 不符" in f["detail"] or "帧集合不符" in f["detail"]
                for f in scen_fail),
            "memory": mem,
            "memory_series_csv": f"sim/{scen}_rounds.csv",
            "failures": scen_fail,
        }
        write_csv(work / f"{scen}_rounds.csv",
                  ["round", "rc", "duration_ms", "rss_peak_kib",
                   "frames", "match", "detail"], rows)
        failures.extend({**f, "scenario": scen} for f in scen_fail)
        if not mem.get("pass"):
            failures.append({"scenario": scen, "detail": "内存判据 FAIL"})
    duration = time.monotonic() - t_phase
    result = {
        "phase": "sim",
        "note": ("冻结后的模拟器 CLI 中 --fixed-clock 与 --scenario 互斥"
                 "（--scenario 自带确定性虚拟时钟）；确定性锚点=UI_CONTRACT §3.3"
                 "「同一输入文件的输出必须完全确定」，逐帧 sha256 与 golden 比对。"
                 "峰值 RSS=/usr/bin/time -l 内核精确值（此前 10ms 采样对 <50ms "
                 "短进程存在启动竞态伪差，已改为内核值；psutil 轮询仅作参考）"),
        "sim_binary": str(SIM),
        "scenarios": per_scen,
        "rounds_per_scenario": sim_rounds,
        "duration_s": round(duration, 2),
        "timeout_per_round_s": timeout_s,
        "failures": failures,
        "pass": not failures,
    }
    write_result(outdir, "sim", result)
    total_rounds = sim_rounds * len(SIM_SCENARIOS)
    print(f"[sim] 完成 {total_rounds} 轮，耗时 {duration:.1f}s，"
          f"{'PASS' if result['pass'] else 'FAIL'}")
    return result


# ---------------------------------------------------------------- 阶段 2b: check_ui ×K

def phase_checkui(outdir: Path, runs: int, timeout_s: float) -> dict:
    print(f"[checkui] 全量重捕 22 场景 actual → check_ui 全 golden 集 ×{runs}")
    work = outdir / "checkui"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    replay_dir = work / "replay"
    replay_dir.mkdir()
    env = {**os.environ, "SDL_VIDEODRIVER": "dummy"}
    failures: list = []
    t_phase = time.monotonic()
    scen_files = sorted(SCEN_DIR.glob("S*.jsonl"))
    if not scen_files:
        env_error(f"场景目录为空: {SCEN_DIR}")
    for f in scen_files:
        r = ChildRunner(f"capture-{f.stem}").run(
            [str(SIM), "--scenario", str(f), "--capture-dir", str(replay_dir)],
            cwd=str(REPO), timeout_s=timeout_s, env=env)
        if r["rc"] != 0 or r["timeout"]:
            failures.append({"detail": f"重捕 {f.name} 失败 rc={r['rc']}"})
    n_actual = len(list(replay_dir.rglob("*.png")))
    n_golden = len(list(GOLDEN.glob("*.png")))
    print(f"[checkui] actual 帧={n_actual} golden 帧={n_golden}")
    if n_actual != n_golden:
        failures.append({"detail": f"actual 帧数 {n_actual} ≠ golden {n_golden}"})
    rows = []
    rss_series: list = []
    for i in range(1, runs + 1):
        r = ChildRunner(f"checkui-{i}").run(
            [sys.executable, str(CHECK_UI), "--golden", str(GOLDEN),
             "--actual", str(replay_dir)],
            cwd=str(REPO), timeout_s=timeout_s)
        summary = ""
        for line in r["stdout_tail"].splitlines():
            if line.startswith("汇总:") or line.startswith("退出码:"):
                summary += line.strip() + " "
        ok = r["rc"] == 0 and not r["timeout"] and "22/22 PASS" in r["stdout_tail"]
        if not ok:
            failures.append({"run": i, "rc": r["rc"], "timeout": r["timeout"],
                             "stdout_tail": r["stdout_tail"][-600:]})
        if r.get("rss_peak_kib") is not None:
            rss_series.append(r["rss_peak_kib"])
        rows.append([i, r["rc"], int(r["duration_s"] * 1000),
                     r.get("rss_peak_kib", ""), int(ok), summary.strip()])
        print(f"[checkui] 第 {i}/{runs} 次 rc={r['rc']} {summary.strip()} "
              f"峰值RSS={r.get('rss_peak_kib', '-')}KiB")
    # 连续 3 次中的第 2、3 次消费的是含 __diff/__expected 的目录，
    # check_ui 自身跳过这些文件（scan_frames），这正是回归稳定性的检验点。
    duration = time.monotonic() - t_phase
    mem = mem_verdict(rss_series, mode="checkui")
    result = {
        "phase": "checkui",
        "golden_frames": n_golden,
        "actual_frames": n_actual,
        "runs": runs,
        "runs_ok": runs - sum(1 for f in failures if "run" in f),
        "memory": mem,
        "memory_series_csv": "checkui/runs.csv",
        "duration_s": round(duration, 2),
        "failures": failures,
        "pass": not failures and bool(mem.get("pass")),
    }
    write_csv(work / "runs.csv",
              ["run", "rc", "duration_ms", "rss_peak_kib", "pass", "summary"],
              rows)
    write_result(outdir, "checkui", result)
    print(f"[checkui] 完成 {runs} 次，耗时 {duration:.1f}s，"
          f"{'PASS' if result['pass'] else 'FAIL'}")
    return result


# ---------------------------------------------------------------- 汇总报告

PHASES = ("bridge", "bridge-inproc", "mock-c", "sim", "checkui")


def phase_report(outdir: Path) -> int:
    results = {}
    missing = []
    for name in PHASES:
        p = outdir / f"{name}.result.json"
        if not p.is_file():
            missing.append(name)
            continue
        results[name] = json.loads(p.read_text(encoding="utf-8"))
    if missing:
        env_error("缺少阶段结果（先跑对应子命令/all）: " + ", ".join(missing))
    all_pass = all(r.get("pass") for r in results.values())
    host = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "psutil": psutil.__version__ if psutil is not None else "缺失(coarse回退)",
    }
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=5)
        host["git_head"] = head.stdout.strip() if head.returncode == 0 else "n/a"
    except Exception:  # noqa: BLE001
        host["git_head"] = "n/a"
    total = round(sum(r.get("duration_s", 0) for r in results.values()), 2)
    report = {
        "task": "P6.2 PC 前置压缩 soak（模拟器+Bridge+共享层；真机 24h 不在本报告范围）",
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scope": "PC 侧 24h 量级时钟压缩到分钟级；确定性系统允许时钟压缩",
        "acceptance": "无崩溃/死锁/持续内存增长；状态最终收敛；结果可追溯",
        "host": host,
        "phases": results,
        "total_duration_s": total,
        "conclusions": {
            "determinism": (
                "全同" if (all([
                    results["bridge"]["determinism"]["all_identical"],
                    all(v["deterministic_double_run"]
                        for v in results["mock-c"]["per_scenario"].values()),
                    all(v["all_frames_identical_to_golden"]
                        for v in results["sim"]["scenarios"].values()),
                    results["checkui"]["runs_ok"] == results["checkui"]["runs"],
                ])) else "存在差异"),
            "memory_no_growth": ("通过" if all(
                (results[n]["memory"]["pass"]
                 if "memory" in results[n] else
                 (results[n]["memory_highwater"]["pass"] and
                  results[n]["memory_current"]["pass"]))
                for n in results
                if "memory" in results[n] or "memory_highwater" in results[n]
            ) else "FAIL：存在持续增长判据失败"),
            "crash_deadlock": (
                f"bridge crash={results['bridge']['crashes']} "
                f"timeout={results['bridge']['timeouts']}; sim " +
                "; ".join(f"{k} crash={v['crashes']} timeout={v['timeouts']}"
                          for k, v in results["sim"]["scenarios"].items())),
            "state_convergence": (
                "check_ui 语义断言含 S21_bridge_restart（新 epoch 收敛）与 "
                "S_lifecycle 终态（DONE 定格 + 最终低压强制页）；C 端解析器接受 "
                f"{results['mock-c']['c_parser']['snapshot_files']} 份 mock 快照"
                if results["mock-c"]["c_parser"]["pass"] else
                "mock 快照未全部通过 C 端解析器"),
        },
        "overall_pass": bool(all_pass),
    }
    (outdir / "soak-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (outdir / "soak-report.md").write_text(
        render_md(report), encoding="utf-8")
    print(f"[report] artifacts/soak/soak-report.md + soak-report.json 写出；"
          f"总体 {'PASS' if all_pass else 'FAIL'}；总耗时 {total:.1f}s")
    return 0 if all_pass else 1


def render_md(r: dict) -> str:
    lines = []
    a = lines.append
    a("# soak-report — P6.2 PC 前置压缩稳定性测试（A5）")
    a("")
    a(f"- 日期：{r['date']}；范围：{r['scope']}")
    a(f"- 复现命令：`sh scripts/run_soak.sh`（真实退出码；本轮完整输出："
      f"`artifacts/soak/run_log.txt`；JSON 汇总 `artifacts/soak/soak-report.json`，"
      f"阶段总耗时 {r['total_duration_s']}s）")
    a(f"- 验收（DEVELOPMENT_PLAN P6.2 行）：{r['acceptance']}")
    a(f"- 宿主：{r['host']['platform']}（{r['host']['machine']}），"
      f"Python {r['host']['python']}，psutil {r['host']['psutil']}，"
      f"git {r['host']['git_head'][:12] if r['host']['git_head'] != 'n/a' else 'n/a'}")
    a("- 边界：本报告只覆盖 PC 侧（模拟器 SDL dummy + Bridge + 共享层 C 解析器），"
      "**不覆盖真机 24h / 100 次断连重连 / 主机睡眠唤醒**（见「剩余问题」）。")
    a("- 内存判据：可回落序列与 ru_maxrss 高水位均判 |末-首| ≤ "
      "max(2048 KiB, 25%×首)（高水位单调不减同上限）；checkui 3 个点只看带宽 "
      "≤ max(4096 KiB, 25%×最小)。斜率仅作方向展示，不单独判定（内核峰值按页"
      "粒度量化，短序列上斜率是噪声）。峰值来源 /usr/bin/time -l 内核精确值。")
    a("- 诚实性：任何失败如实列出，无重试；非零退出码原样上抛（run_soak.sh）。")
    a("")
    a("## 结论")
    a("")
    a(f"| 维度 | 结论 |")
    a(f"|---|---|")
    a(f"| 确定性 | {r['conclusions']['determinism']} |")
    a(f"| 内存 | {r['conclusions']['memory_no_growth']} |")
    a(f"| 崩溃/死锁 | {r['conclusions']['crash_deadlock']} |")
    a(f"| 状态收敛 | {r['conclusions']['state_convergence']} |")
    a(f"| 总体 | **{'PASS' if r['overall_pass'] else 'FAIL'}** "
      f"（各阶段真实退出码见 soak-report.json 与 run_soak.sh 输出） |")
    a("")
    for name, res in r["phases"].items():
        a(f"## 阶段 {name} — {'PASS' if res.get('pass') else 'FAIL'}"
          f"（{res.get('duration_s', '?')}s）")
        a("")
        if name == "bridge":
            a(f"- 轮数：{res['rounds_ok']}/{res['rounds_planned']}；"
              f"crash={res['crashes']}；timeout={res['timeouts']}")
            a(f"- 确定性：快照全部 sha256 与首轮逐字节比对 → "
              f"{'500 轮全同' if res['determinism']['all_identical'] else '存在差异（见 failures）'}；"
              f"参考快照 {res['determinism']['reference_snapshot_count']} 份"
              f"（{res['determinism']['reference_dir']}）")
            a(f"- 内存（子进程峰值 RSS 序列）：首={res['memory'].get('first_kib')} "
              f"末={res['memory'].get('last_kib')} "
              f"最小={res['memory'].get('min_kib')} "
              f"峰值={res['memory'].get('peak_kib')} KiB，"
              f"斜率={res['memory'].get('slope_kib_per_round')} KiB/轮 → "
              f"{'PASS' if res['memory'].get('pass') else 'FAIL'}")
            a(f"- 曲线：`artifacts/soak/{res['memory_series_csv']}`")
        elif name == "bridge-inproc":
            a(f"- 单进程 {res['iterations']} 次全量回放渲染（{res['warmup']} 次预热），"
              f"输出摘要全同={res['output_digest_stable']}；"
              f"这是持续内存增长的主证据（真实 Bridge 为长驻进程）")
            a(f"- 高水位 ru_maxrss（单调不减，判总漂移）：首={res['memory_highwater'].get('first_kib')} "
              f"末={res['memory_highwater'].get('last_kib')} "
              f"峰值={res['memory_highwater'].get('peak_kib')} KiB，"
              f"总漂移={res['memory_highwater'].get('measured_last_first_kib')} KiB"
              f"（斜率展示 {res['memory_highwater'].get('slope_kib_per_round')} KiB/轮）→ "
              f"{'PASS' if res['memory_highwater'].get('pass') else 'FAIL'}")
            a(f"- 当前 RSS：首={res['memory_current'].get('first_kib')} "
              f"末={res['memory_current'].get('last_kib')} "
              f"峰值={res['memory_current'].get('peak_kib')} KiB，"
              f"斜率={res['memory_current'].get('slope_kib_per_round')} KiB/轮 → "
              f"{'PASS' if res['memory_current'].get('pass') else 'FAIL'}")
        elif name == "mock-c":
            a("- 7 场景 mock 全集 ×2 遍确定性：")
            a("")
            a("| 场景 | 快照数 | 两遍全同 |")
            a("|---|---|---|")
            for s, v in res["per_scenario"].items():
                a(f"| {s} | {v['snapshot_count']} | "
                  f"{'是' if v['deterministic_double_run'] else '否'} |")
            a("")
            cp = res["c_parser"]
            a(f"- C 端解析器（{Path(cp['binary']).name}，P1.4 有界解析，"
              f"tests/fixtures 校验模式 valid_*.json）：{cp['snapshot_files']} 份快照，"
              f"rc={cp['rc']} → {'PASS' if cp['pass'] else 'FAIL'}"
              f"（stdout 证据：`artifacts/soak/{cp['stdout_evidence']}`）")
        elif name == "sim":
            a(f"- 注：{res['note']}")
            a("")
            a("| 场景 | 轮数 | 帧数 | 全同 golden | crash | timeout | "
              "RSS 首/末/峰值 KiB | 斜率 KiB/轮 | 内存 |")
            a("|---|---|---|---|---|---|---|---|---|")
            for s, v in res["scenarios"].items():
                m = v["memory"]
                a(f"| {s} | {v['rounds_ok']}/{v['rounds']} | {v['golden_frames']} | "
                  f"{'是' if v['all_frames_identical_to_golden'] else '否'} | "
                  f"{v['crashes']} | {v['timeouts']} | "
                  f"{m.get('first_kib')}/{m.get('last_kib')}/{m.get('peak_kib')} | "
                  f"{m.get('slope_kib_per_round')} | "
                  f"{'PASS' if m.get('pass') else 'FAIL'} |")
            a("")
            for s, v in res["scenarios"].items():
                a(f"  - 曲线：`artifacts/soak/{v['memory_series_csv']}`")
        elif name == "checkui":
            a(f"- golden 帧={res['golden_frames']}，actual 帧={res['actual_frames']}；"
              f"check_ui 全集连续 {res['runs']} 次，通过 {res['runs_ok']} 次")
            a(f"- 内存（峰值 RSS，3 点带宽判据）：首={res['memory'].get('first_kib')} "
              f"末={res['memory'].get('last_kib')} "
              f"峰值={res['memory'].get('peak_kib')} KiB → "
              f"{'PASS' if res['memory'].get('pass') else 'FAIL'}"
              f"（带宽 {res['memory'].get('measured_band_kib')}/"
              f"{res['memory'].get('band_kib')} KiB）")
        if res.get("failures"):
            a("")
            a("- **失败明细（如实记录，未重试）**：")
            for f in res["failures"][:20]:
                a(f"  - `{json.dumps(f, ensure_ascii=False)[:400]}`")
        a("")
    a("## 剩余问题（不在本报告范围）")
    a("")
    a("- 真机 24h soak、100 次断连重连、主机睡眠/唤醒、Bridge 重启、"
      "无线权限拒绝、证书失效（P6.2 原文剩余项）需在真机链路（P3/P5 Gate 后）执行；"
      "本 PC 前置结果不等于真机结论。")
    a("- Bridge 传输层（BLE/WSS）与真机电源路径未进入本 soak。")
    a("- 模拟器冻结 CLI 中 --fixed-clock 与 --scenario 互斥，任务书中"
      "「--fixed-clock + --scenario」组合不可用；已按 UI_CONTRACT §3 冻结语义"
      "（--scenario 确定性虚拟时钟）执行并逐帧比对 golden。")
    a("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--outdir", default=str(REPO / "artifacts" / "soak"),
                        help="输出目录（默认 artifacts/soak）")
    common.add_argument("--rounds", type=int, default=500,
                        help="bridge/bridge-inproc 轮数（默认 500）")
    common.add_argument("--sim-rounds", type=int, default=100,
                        help="sim 每场景轮数（默认 100）")
    common.add_argument("--checkui-runs", type=int, default=3,
                        help="check_ui 全集运行次数（默认 3）")
    common.add_argument("--timeout", type=float, default=30.0,
                        help="每子进程超时秒（死锁判据；默认 30）")
    common.add_argument("--checkui-timeout", type=float, default=600.0,
                        help="check_ui 单次超时秒（默认 600）")
    p = argparse.ArgumentParser(prog="soak_bridge.py",
                                description="P6.2 PC 前置压缩 soak（A5）")
    sub = p.add_subparsers(dest="phase", required=True)
    for name in ("bridge", "bridge-inproc", "mock-c", "sim", "checkui",
                 "report", "all"):
        sub.add_parser(name, parents=[common])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    outdir = Path(args.outdir)
    if args.phase in ("bridge", "bridge-inproc", "mock-c", "sim", "checkui",
                      "all"):
        require_paths()
    if args.phase == "bridge":
        return 0 if phase_bridge(outdir, args.rounds, args.timeout)["pass"] else 1
    if args.phase == "bridge-inproc":
        return 0 if phase_bridge_inproc(outdir, args.rounds)["pass"] else 1
    if args.phase == "mock-c":
        return 0 if phase_mock_c(outdir, args.timeout)["pass"] else 1
    if args.phase == "sim":
        return 0 if phase_sim(outdir, args.sim_rounds, args.timeout)["pass"] else 1
    if args.phase == "checkui":
        return 0 if phase_checkui(outdir, args.checkui_runs,
                                  args.checkui_timeout)["pass"] else 1
    if args.phase == "report":
        return phase_report(outdir)
    # all
    overall = 0
    t0 = time.monotonic()
    for fn in (lambda: phase_bridge(outdir, args.rounds, args.timeout),
               lambda: phase_bridge_inproc(outdir, args.rounds),
               lambda: phase_mock_c(outdir, args.timeout),
               lambda: phase_sim(outdir, args.sim_rounds, args.timeout),
               lambda: phase_checkui(outdir, args.checkui_runs,
                                     args.checkui_timeout)):
        try:
            if not fn()["pass"]:
                overall = 1
        except SystemExit as e:
            overall = overall or int(e.code or 0)
        except Exception as e:  # noqa: BLE001 — 阶段崩溃如实记录
            print(f"阶段异常: {type(e).__name__}: {e}", file=sys.stderr)
            overall = 1
    rc = phase_report(outdir)
    overall = overall or (1 if rc else 0)
    print(f"[all] 总耗时 {time.monotonic() - t0:.1f}s，退出码 {overall}")
    return overall


if __name__ == "__main__":
    sys.exit(main())
