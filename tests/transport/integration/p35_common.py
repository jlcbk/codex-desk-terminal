"""p35_common.py — P3.5 共同故障集成测试共用工具（A4+A5，host，无真机）。

职责（任务书第 1–2 节）：
  - 构造 schema 形状的 mock AppState 快照字节（epoch/seq/线程/项目/活动可控，
    逐字节确定性），作为「mock 快照序列 + 故障注入」的原料；
  - 把注入序列写成 C harness 容器（snapshot=完整快照（WSS 设备侧等价路径）、
    fragment=完整 BLE DATA 帧（BLE 设备侧等价路径）、probe=只观测点），
    运行 tests/transport/integration/test_p35_harness.c 产出 JSON 报告；
  - 终点断言辅助：link 事件序列比对（INTERFACES §4 connected→stale→
    disconnected 及恢复）；
  - transport 生命周期记录器（切换测试）：记录 start/stop begin/end 时刻，
    计算活跃区间并检测重叠（重叠即 FAIL，检测器自带自检见 test_switch.py）。

判定层级：设备侧终点 = C harness 内的真实 cdt_reassembler + cdt_state_store +
cdt_present（UI 不回退在 harness 内用真实 presenter 输出判定，报告字段
ui_no_regress）；本模块只构造输入与断言输出，不重实现任何冻结语义。

证据目录：artifacts/transport/p35/<CASE>/（容器、报告、Python 侧轨迹）。
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import time
from typing import List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
ART_ROOT = os.path.join(REPO, "artifacts", "transport", "p35")
HARNESS_BIN = os.path.join(REPO, "build", "transport", "p35", "test_p35_harness")
HARNESS_SRC = os.path.join(REPO, "tests", "transport", "integration", "test_p35_harness.c")

# 容器动作码（与 test_p35_harness.c 冻结一致）
ACT_SNAPSHOT = 1
ACT_FRAGMENT = 2
ACT_PROBE = 3
TRANSPORT_CODES = {"mock": 0, "ble": 1, "wifi": 2}

# §4 冻结阈值（毫秒；harness 内同值）
LINK_STALE_MS = 45000
LINK_DISCONNECTED_MS = 150000


# ---------------------------------------------------------------------------
# mock 快照构造（schema 形状；全部必填键齐全，通过 cdt_parser 才有效）
# ---------------------------------------------------------------------------


def make_state(
    epoch: str = "run-1",
    seq: int = 1,
    *,
    thread_id: str = "thread-demo",
    project: str = "codex-desk-terminal",
    activity: str = "演示活动：检查需求",
    state: str = "working",
    turn_id: Optional[str] = "turn-001",
) -> bytes:
    """构造一份完整 AppState JSON 字节（测试夹具；形状与 INTERFACES §2 一致）。"""
    snapshot = {
        "schema_version": 1,
        "kind": "state",
        "bridge_epoch": epoch,
        "seq": seq,
        "generated_at_ms": 1789000000000 + seq,
        "source": {
            "kind": "mock",
            "connected": True,
            "stale": False,
            "last_event_at_ms": 1789000000000,
        },
        "selected_thread_id": thread_id,
        "threads_total": 1,
        "threads_truncated": False,
        "threads": [
            {
                "id": thread_id,
                "turn_id": turn_id,
                "project": project,
                "state": state,
                "activity": activity,
                "updated_at_ms": 1789000000000,
                "elapsed_ms": 1000,
                "waiting_ms": 0,
                "end_reason": None,
                "attention": None,
                "plan": {"total": 0, "truncated": False, "steps": []},
                "context": {
                    "used_tokens": None,
                    "capacity_tokens": None,
                    "used_percent": None,
                },
            }
        ],
        "usage": {
            "available": False,
            "updated_at_ms": None,
            "windows_total": 0,
            "windows_truncated": False,
            "windows": [],
        },
    }
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# 注入容器与 harness 驱动
# ---------------------------------------------------------------------------

# 条目：(action, at_ms, payload)；payload 为快照字节 / 完整 DATA 帧 / None(probe)
Entry = Tuple[int, int, Optional[bytes]]


def snap(at_ms: int, payload: bytes) -> Entry:
    return (ACT_SNAPSHOT, at_ms, payload)


def frag(at_ms: int, frame: bytes) -> Entry:
    return (ACT_FRAGMENT, at_ms, frame)


def probe(at_ms: int) -> Entry:
    return (ACT_PROBE, at_ms, None)


def write_container(path: str, mtu: int, transport: str, entries: Sequence[Entry]) -> int:
    """写 C harness 注入容器（二进制格式见 test_p35_harness.c 文件头）。"""
    prev_ms = -1
    with open(path, "wb") as fh:
        fh.write(b"P35C")
        fh.write(struct.pack("<HHBBH", 1, mtu, TRANSPORT_CODES[transport], 0, 0))
        for action, at_ms, payload in entries:
            if at_ms < prev_ms:
                raise ValueError("容器 at_ms 回退（虚拟时钟必须单调不减）")
            prev_ms = at_ms
            data = payload or b""
            fh.write(struct.pack("<BxQI", action, at_ms, len(data)))
            fh.write(data)
    return len(entries)


def build_harness(binary: str = HARNESS_BIN, force: bool = False) -> str:
    """编译 C harness（与 scripts/run_p35.sh 同参数；源码较新或缺失时重建）。"""
    srcs = [
        os.path.join(REPO, "shared", "transport", "cdt_crc32.c"),
        os.path.join(REPO, "shared", "transport", "cdt_fragmenter.c"),
        os.path.join(REPO, "shared", "transport", "cdt_reassembler.c"),
        os.path.join(REPO, "shared", "state", "cdt_json.c"),
        os.path.join(REPO, "shared", "state", "cdt_parser.c"),
        os.path.join(REPO, "shared", "state", "cdt_store.c"),
        os.path.join(REPO, "shared", "presenter", "cdt_presenter.c"),
        HARNESS_SRC,
    ]
    stale = force or not os.path.isfile(binary)
    if not stale:
        bt = os.path.getmtime(binary)
        stale = any(os.path.getmtime(s) > bt for s in srcs)
    if stale:
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        cmd = ["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-pedantic",
               "-I" + os.path.join(REPO, "shared", "transport"),
               "-I" + os.path.join(REPO, "shared", "state"),
               "-I" + os.path.join(REPO, "shared", "presenter")]
        cmd += srcs + ["-o", binary]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("harness 编译失败：\n%s%s" % (proc.stdout, proc.stderr))
    return binary


def run_harness(binary: str, container: str, report_path: str) -> dict:
    """运行 harness 并返回解析后的 JSON 报告（退出码非 0 视为环境错误）。"""
    proc = subprocess.run([binary, "--run", container, report_path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("harness 退出码 %d（容器/环境错误）：%s%s"
                             % (proc.returncode, proc.stdout, proc.stderr))
    with open(report_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def case_dir(case_id: str) -> str:
    """证据目录 artifacts/transport/p35/<CASE>/。"""
    d = os.path.join(ART_ROOT, case_id)
    os.makedirs(d, exist_ok=True)
    return d


def run_case(case_id: str, mtu: int, transport: str, entries: Sequence[Entry],
             binary: str = HARNESS_BIN) -> dict:
    """一条注入序列 → 容器 + harness 报告（证据落盘）→ 返回报告。"""
    d = case_dir(case_id)
    container = os.path.join(d, "container.bin")
    report = os.path.join(d, "report.json")
    n = write_container(container, mtu, transport, entries)
    out = run_harness(binary, container, report)
    with open(os.path.join(d, "container.json"), "w", encoding="utf-8") as fh:
        json.dump({"case": case_id, "mtu": mtu, "transport": transport, "entries": n,
                   "actions": [{"at_ms": e[1],
                                "action": {1: "snapshot", 2: "fragment",
                                           3: "probe"}[e[0]]} for e in entries]},
                  fh, ensure_ascii=False, indent=1)
    return out


# ---------------------------------------------------------------------------
# 终点断言辅助
# ---------------------------------------------------------------------------


def assert_link_trace(report: dict, expected: Sequence[Tuple[int, str]], ctx: str = "") -> None:
    """链路事件序列精确比对（§4：事件序列记录在报告 link_events）。"""
    got = [(e["at_ms"], e["state"]) for e in report["link_events"]]
    assert got == list(expected), (
        "%s link 事件序列不符（§4）：got=%r expected=%r" % (ctx, got, list(expected)))


def applied_pairs(report: dict) -> List[Tuple[str, int]]:
    """applied 轨迹压缩为 (epoch, seq) 列表（顺序即应用顺序）。"""
    return [(a["epoch"], a["seq"]) for a in report["applied"]]


def dump_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, default=str)


# ---------------------------------------------------------------------------
# transport 生命周期记录（切换测试：一次仅一个活动 transport）
# ---------------------------------------------------------------------------


class LifecycleLog:
    """start/stop begin/end 时刻记录器（time.monotonic；真机等价钩子）。

    事件：(t, kind, name)，kind ∈ start_begin/start_end/stop_begin/stop_end。
    活跃区间 = [start_begin, stop_end)（未 stop 则到 +inf）。
    """

    def __init__(self) -> None:
        self.events: List[Tuple[float, str, str]] = []

    def _rec(self, kind: str, name: str) -> None:
        self.events.append((time.monotonic(), kind, name))

    def start_begin(self, name: str) -> None:
        self._rec("start_begin", name)

    def start_end(self, name: str) -> None:
        self._rec("start_end", name)

    def stop_begin(self, name: str) -> None:
        self._rec("stop_begin", name)

    def stop_end(self, name: str) -> None:
        self._rec("stop_end", name)

    def intervals(self) -> List[Tuple[str, float, float]]:
        """按 transport 名配对 start_begin/stop_end → 活跃区间（秒，monotonic）。"""
        begins: dict = {}
        out: List[Tuple[str, float, float]] = []
        for t, kind, name in self.events:
            if kind == "start_begin":
                if name in begins:
                    raise AssertionError("transport %s 重复 start_begin" % name)
                begins[name] = t
            elif kind == "stop_end":
                if name not in begins:
                    raise AssertionError("transport %s 无 start_begin 即 stop_end" % name)
                out.append((name, begins.pop(name), t))
        if begins:
            for name, t in begins.items():
                out.append((name, t, float("inf")))
        return out


def assert_single_active(log: LifecycleLog) -> List[Tuple[str, float, float]]:
    """断言任意时刻至多一个活动 transport（区间两两无重叠）。

    返回区间列表（供证据落盘）；检测到重叠/事件失配即 AssertionError（FAIL）。
    """
    intervals = log.intervals()
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            n1, s1, e1 = intervals[i]
            n2, s2, e2 = intervals[j]
            if s1 < e2 and s2 < e1:
                raise AssertionError(
                    "检测到 transport 生命周期重叠：%s [%.6f, %.6f) × %s [%.6f, %.6f)"
                    % (n1, s1, e1, n2, s2, e2))
    return intervals
