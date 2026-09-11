"""test_continuous_orchestration.py — R6 同实例持续编排测试（A5，PC 级，纯软件）。

对应 docs/ARCHITECTURE_REVIEW_2026-09-11.md R6 / P2：现有 soak
（scripts/soak_bridge.py）每轮新建 StateEngine/新模拟器进程，验证的是确定性
与短周期重复，不证明同一生产编排对象持续运行不会累积错误（R3 即「组件全过、
main 编排失效」实例）。本测试用**同一组生产对象贯穿全程**：

  - 一个 StateEngine（bridge/state/engine.py 真实现，全程不重建、epoch 恒定）；
  - 一个 WSS server（bridge/transports/wss/server.py 真实现；127.0.0.1 预留
    临时端口，**不用 8765**（P5.2 域在用）；明文 ws 仅 loopback 且显式
    allow_insecure_loopback/dev_insecure_loopback，同 P3.3/P3.5 夹具）；
  - 一个订户（bridge/transports/wss/client_mock.py 真实现，同一对象经历全部
    故障；Transport 语义零改动，本文件只做字节级观测与断言）；
  - 事件源复用 mock/replay 的 NormalizedEvent 构造器，但由本测试的编排泵
    **连续 apply 到同一 engine** 并经 server.send 推送（生产 main 编排等价物）。

注入时间线（虚拟单调时钟，总跨度 ≥600000 ms = 10 分钟等效；真实墙钟压缩运行）：
  E0 启动 + 首份全量；E1 多 thread/turn 生命周期（WORKING→NEEDS YOU→等待
  解除→DONE）；E2 重复 seq 洪泛（同字节重发 ×40 → server 去重）+ 乱序（旧
  seq 重发 ×3）；E3 桥重启 ×3（同一 WssServer 对象同端口 stop/start；engine
  **不**重建；server down 期间编排照常 apply→BUSY 停靠，重连后收停靠前最新
  全量）；E4 断连退避重连 ×5（server 侧 TCP abort = 网络失联 1006 语义；同一
  订户对象按冻结形状的测试缩短退避档重连，每次取得最新全量）；E5 审批
  pending→resolve（×2 部分解除）、跨任务多线程、新 turn 不继承 DONE、迟到
  事件门闸、usage/额度更新；E6 上游断连/重连（source_connected/stale，任务态
  不被伪造）；E7 电池 trace 独立驱动（S09 类 3980→3600 mV，临界段 1 Hz 虚拟
  连续；经上行 DeviceTelemetry 独立通道 ≤512 B，不进入 State）；E8 最终收敛。

断言（真实退出码驱动）：
  - 每轮状态收敛：检查点快照字段断言 + protocol/state.schema.json 校验；
  - seq/epoch 无回退：订户侧对账器逐帧核对（线路序中 seq 回退只允许出现在
    注入的 3 帧乱序；epoch 全程恒定）；
  - engine 内部计数有界：threads 数 == 注入去重数、pending 全清、
    server 缺陷计数（oversize/non_utf8/401/403/404）与订户缺陷计数
    （config_error/oversize/CONFIG_ERROR 后重试）全零；
  - 对账：订户收帧列表 == 模型期望帧列表（逐字节、按序，含重连全量帧），
    server.stats.snapshots_pushed == 订户 messages_received，上行遥测逐条且
    字节一致——无静默丢帧、无凭空造帧（收到帧 ⊆ engine 产出集）；
  - RSS：本 pytest 进程检查点序列 + ru_maxrss 高水位，复用 soak_bridge.py 的
    mem_verdict 判据口径（importlib 直接加载同一实现，不复制阈值）。

边界声明（与报告一致）：
  1. 本测试只覆盖 **Bridge 侧同实例编排**（PC 级）；**固件 main 的同实例持续
     验证归 24h 真机 soak（P6.2 剩余项）**，本测试不假装覆盖无线/电源/LCD/ADC。
  2. 与 scripts/soak_bridge.py **互补不替代**：soak = 确定性回归（每轮新
     engine/新进程）；本测试 = 同实例故障编排（同一 engine/server/订户连续
     经历故障）。
  3. S21 类「桥重启 = 新 epoch + seq 归零」在真实生产中伴随 engine 重建；本
     测试刻意不重建 engine，断言重启间 epoch 恒定、seq 严格递增（更强的同实例
     性质）；epoch 交替的订户合法性由对账器自检（合成帧，不走线路）单独覆盖。
  4. 断连注入读取 server._conns 并对连接 transport.abort()（私有面）——等价
     拔网线的故障注入，不修改任何生产代码语义；发现的生产代码问题只报告不改。
  5. 模拟器长驻进程为任务可选项，未包含（UI/sim soak 已由 P6.2 覆盖；本测试
     聚焦 Bridge 侧编排）。
  6. server 存活快照心跳（keepalive）置 None：周期推送语义由 P3.3/P3.5 覆盖，
     本测试要求静止点对账确定性。

入口：sh scripts/run_continuous_test.sh（真实退出码；产物 artifacts/continuous/）。
退出码：pytest 0 = 全部通过；1 = 断言失败；2 = 环境错误（依赖缺失等）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import platform
import socket
import sys
import time
from typing import Callable, Optional

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # 直跑（非 pytest）兜底
    sys.path.insert(0, str(REPO))

from bridge import events as ev  # noqa: E402
from bridge.state.engine import StateEngine  # noqa: E402
from bridge.transports.wss import (  # noqa: E402
    BackoffConfig,
    LinkState,
    MockDeviceClient,
    WssClientConfig,
    WssServer,
    WssServerConfig,
)
from bridge.transports.wss.server import SendStatus as ServerSendStatus  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ART = REPO / "artifacts" / "continuous"
STATE_SCHEMA = REPO / "protocol" / "state.schema.json"
TELEMETRY_SCHEMA = REPO / "protocol" / "telemetry.schema.json"

EPOCH = "cont-orch-001"
ANCHOR_MS = 1789000000000          # 与 SCENARIOS.md §2 虚构 UTC 基准一致
TOKEN = "r6-continuous-dev-token"  # 测试注入凭证，不写入任何日志输出
# 测试退避：形状与冻结序列一致（1/2/4/8/16/30s 的等比缩短档，同 P3.5 夹具）
TEST_BACKOFF = BackoffConfig(sequence_s=(0.05, 0.1, 0.2, 0.4, 0.8, 1.0))
MIN_VIRTUAL_SPAN_MS = 600000       # ≥10 分钟等效（虚拟时钟）
WAIT_S = 10.0

# 虚拟时间线锚点（单调毫秒）
T_E1 = 1000
T_E2 = 20000
T_E3 = [30000, 40000, 50000]
T_E4 = [60000, 62000, 64000, 66000, 68000]
T_E5 = 70000
T_E6 = 100000
T_E7 = 200000
T_E8 = 660000
VIRTUAL_SPAN_MS = T_E8 + 200


def _load_soak_mem():
    """直接加载 scripts/soak_bridge.py 复用内存判据实现（不复制阈值）。"""
    spec = importlib.util.spec_from_file_location(
        "soak_bridge_for_r6", REPO / "scripts" / "soak_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# schema 校验（惰性；jsonschema 由运行入口注入）
# ---------------------------------------------------------------------------

_VAL: Optional[dict] = None


def _validators() -> dict:
    global _VAL
    if _VAL is None:
        from jsonschema.validators import Draft202012Validator
        state_schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
        tel_schema = json.loads(TELEMETRY_SCHEMA.read_text(encoding="utf-8"))
        _VAL = {
            "state": Draft202012Validator(state_schema),
            "tel": Draft202012Validator(tel_schema),
        }
    return _VAL


def validate_state(frame: dict, where: str) -> None:
    errs = sorted(_validators()["state"].iter_errors(frame), key=lambda e: list(e.path))
    assert not errs, f"{where}: AppState schema 违例: {errs[0].message[:200]}"


def validate_tel(payload: bytes, where: str) -> None:
    d = json.loads(payload.decode("utf-8"))
    errs = sorted(_validators()["tel"].iter_errors(d), key=lambda e: list(e.path))
    assert not errs, f"{where}: DeviceTelemetry schema 违例: {errs[0].message[:200]}"


async def wait_for(predicate: Callable[[], bool], timeout: float = WAIT_S,
                   interval: float = 0.02, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"超时等待 {what}（{timeout:.1f}s）")


def reserve_loopback_port() -> int:
    """预留一个 loopback 临时端口（**不用 8765**——P5.2 域在用）。

    桥重启 ×3 需要"同端口"，不能用 port=0（每次 start 会换端口）；先
    绑定-取号-关闭再启动（进程内随即占用，竞争窗口极小）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 观测件：字节级对账模型 + 订户侧 (epoch, seq) 对账器
# ---------------------------------------------------------------------------


class DeliveryModel:
    """server 下发字节流的对账模型（与 WssServer 存储/去重/重连全量语义同构）。

    - server 存活时 publish：字节存为 _current；已连接订户上与该连接上次已推
      字节相同 → 去重（不产生帧）；否则推帧。
    - server 停机时 publish：send 返回 BUSY，字节**不**被存储（停靠计数）。
    - 订户（重）连入：server 立即推送 _current 全量（新连接无去重基线）。
    静止点（quiesce）保证模型序 == 线路序。
    """

    def __init__(self) -> None:
        self.expected: list[bytes] = []
        self.stored_current: Optional[bytes] = None
        self.conn_last: Optional[bytes] = None
        self.client_up = False
        self.dedup_expected = 0
        self.parked = 0  # server down 期间 publish（BUSY，永不达线路）

    def on_client_connected(self) -> None:
        self.client_up = True
        if self.stored_current is not None:
            self.expected.append(self.stored_current)
            self.conn_last = self.stored_current

    def on_client_down(self) -> None:
        self.client_up = False
        self.conn_last = None  # server 侧连接条目被清理；重连后从无基线起步

    def on_publish(self, raw: bytes, *, server_up: bool) -> None:
        if not server_up:
            self.parked += 1
            return
        self.stored_current = raw
        if self.client_up:
            if raw == self.conn_last:
                self.dedup_expected += 1
            else:
                self.expected.append(raw)
                self.conn_last = raw


class Reconciler:
    """订户侧 (epoch, seq) 轨迹观测（只读，不改变订户行为）。

    - 新 epoch 首帧：合法全量替换（S21 语义），重置该 epoch 的 seq 基线；
    - seq == 当前最大：重投（重连全量重发同一快照，冻结语义「重连总是取得
      全量」的合法表现），单独计数，不更新最新应用帧；
    - seq < 当前最大：乱序/回退（stale），单独计数，不更新最新应用帧。
    """

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.epoch_max: dict[str, int] = {}
        self.stale: list[tuple[str, int, int]] = []        # seq 回退（乱序注入）
        self.redeliveries: list[tuple[str, int]] = []      # seq == max（重连全量重投）
        self.epoch_switches = 0
        self.latest: Optional[dict] = None

    def note(self, raw: bytes) -> dict:
        self.frames.append(raw)
        d = json.loads(raw.decode("utf-8"))
        ep, seq = d["bridge_epoch"], d["seq"]
        seen = self.epoch_max.get(ep)
        if seen is None:
            if self.epoch_max:
                self.epoch_switches += 1
            self.epoch_max[ep] = seq
            self.latest = d
        elif seq > seen:
            self.epoch_max[ep] = seq
            self.latest = d
        elif seq == seen:
            self.redeliveries.append((ep, seq))
        else:
            self.stale.append((ep, seq, seen))
        return d

    def self_check(self) -> None:
        """对账器自检（合成帧，不走线路）：epoch 交替合法 + stale 判定。"""
        alt = self.__class__()
        a5 = json.dumps({"bridge_epoch": "A", "seq": 5}).encode()
        b1 = json.dumps({"bridge_epoch": "B", "seq": 1}).encode()
        b2 = json.dumps({"bridge_epoch": "B", "seq": 2}).encode()
        for f in (a5, b1, b2, b1):
            alt.note(f)
        assert alt.epoch_switches == 1, alt.epoch_switches
        assert alt.stale == [("B", 1, 2)], alt.stale
        assert alt.latest == {"bridge_epoch": "B", "seq": 2}, alt.latest


# ---------------------------------------------------------------------------
# 编排器：同一组生产对象 + 事件泵 + 检查点
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.soak = _load_soak_mem()
        try:
            import psutil
            self._proc = psutil.Process()
        except ImportError:
            self._proc = None
        self.t_wall0 = time.monotonic()

        # -- 生产对象（构造一次，全程复用；engine 全程不重建） --
        self.engine = StateEngine(EPOCH, source_kind="mock", utc_anchor_ms=ANCHOR_MS)
        self.port = reserve_loopback_port()
        self.server_up = False
        self.model = DeliveryModel()
        self.rec = Reconciler()
        self.server_link_log: list[tuple[str, object]] = []
        self.client_link_log: list[str] = []
        self.server = WssServer(
            WssServerConfig(host="127.0.0.1", port=self.port, max_clients=1,
                            keepalive_interval_s=None,  # 见 docstring 边界 6
                            allow_insecure_loopback=True),
            device_token=TOKEN,
            on_message=self._on_uplink,
            on_link=self._on_server_link,
        )
        self.client = MockDeviceClient(
            WssClientConfig(url=f"ws://127.0.0.1:{self.port}/v1/state",
                            dev_insecure_loopback=True, backoff=TEST_BACKOFF,
                            connect_timeout_s=5.0),
            device_token=TOKEN,
            on_message=self._on_downlink,
            on_link=self._on_client_link,
        )
        self.client_task: Optional[asyncio.Task] = None

        self.produced: set[bytes] = set()
        self.history: list[tuple[int, bytes]] = []  # (mono, raw) 全部产出
        self.applied = 0
        self.busy_parked_events: list[dict] = []
        self.tel_sent: list[bytes] = []
        self.tel_received: list[bytes] = []
        self.tel_missed: list[dict] = []
        self.rss_series: list[float] = []
        self.checkpoints: list[dict] = []

    # -- 回调（Transport → 测试观测，只读） --

    def _on_server_link(self, status: str, detail) -> None:
        self.server_link_log.append((status, detail))

    def _on_client_link(self, state: LinkState, detail=None) -> None:
        self.client_link_log.append(state.value)
        if state is LinkState.CONNECTED:
            self.model.on_client_connected()
        elif state in (LinkState.DISCONNECTED, LinkState.CONFIG_ERROR,
                       LinkState.STOPPED):
            self.model.on_client_down()

    def _on_downlink(self, data: bytes, n: int) -> None:
        self.rec.note(bytes(data))

    def _on_uplink(self, data: bytes, n: int) -> None:
        self.tel_received.append(bytes(data))

    # -- 基础操作 --

    def rss_kib(self) -> Optional[float]:
        if self._proc is None:
            return None
        return self._proc.memory_info().rss / 1024.0

    def wall_s(self) -> float:
        return round(time.monotonic() - self.t_wall0, 3)

    async def start_all(self) -> None:
        await self.server.start()
        self.server_up = True
        self.client_task = asyncio.create_task(self.client.run(),
                                               name="r6-subscriber")

    async def publish(self, event, mono: int) -> dict:
        """生产编排等价物：同一 engine 连续 apply → server.send 最新全量字节。"""
        snap = self.engine.apply(event, mono)
        self.applied += 1
        raw = json.dumps(snap, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        self.produced.add(raw)
        self.history.append((mono, raw))
        up = self.server_up
        if up:
            status = await self.server.send(raw)
            assert status is ServerSendStatus.ACCEPTED, status
        else:
            self.busy_parked_events.append(
                {"mono": mono, "type": event.type, "seq": snap["seq"]})
        self.model.on_publish(raw, server_up=up)
        return snap

    async def resend(self, raw: bytes) -> None:
        """编排层故障注入：重发历史字节（洪泛/乱序），不经 engine。"""
        assert self.server_up
        status = await self.server.send(raw)
        assert status is ServerSendStatus.ACCEPTED, status
        self.model.on_publish(raw, server_up=True)

    async def quiesce(self) -> None:
        """静止点：等待模型期望帧全部抵达订户（对账前提）。"""
        want = sum(len(f) for f in self.model.expected)
        await wait_for(
            lambda: self.client.stats.bytes_received >= want,
            what=f"订户收满 {want} 字节（当前 {self.client.stats.bytes_received}）")
        diff = next((i for i, (x, y) in
                     enumerate(zip(self.rec.frames, self.model.expected)) if x != y),
                    None if len(self.rec.frames) == len(self.model.expected)
                    else min(len(self.rec.frames), len(self.model.expected)))
        assert self.rec.frames == self.model.expected, (
            f"帧序列与模型不符：收到 {len(self.rec.frames)} 帧 / 期望 "
            f"{len(self.model.expected)} 帧，首个差异下标 {diff}")

    async def checkpoint(self, result: dict, episode: str, name: str, mono: int,
                         expect: Optional[dict] = None) -> dict:
        """检查点：静止 → 最新帧字段断言 + schema 校验 → 记录时间线与 RSS。"""
        await self.quiesce()
        frame = self.rec.latest
        assert frame is not None, f"{episode}/{name}: 无最新帧"
        assert frame["bridge_epoch"] == EPOCH, (
            f"{episode}/{name}: epoch 漂移 {frame['bridge_epoch']!r}")
        assert frame["seq"] == self.engine.seq, (
            f"{episode}/{name}: 最新帧 seq={frame['seq']} != "
            f"engine.seq={self.engine.seq}")
        if expect:
            for path, want in expect.items():
                got: object = frame
                for key in path.split("."):
                    if isinstance(got, list):
                        got = got[int(key)]
                    else:
                        got = got[key]
                assert got == want, (
                    f"{episode}/{name}: 字段 {path} 期望 {want!r} 实得 {got!r}")
        validate_state(frame, f"{episode}/{name}")
        rss = self.rss_kib()
        if rss is not None:
            self.rss_series.append(rss)
        cp = {"episode": episode, "name": name, "mono_ms": mono,
              "wall_s": self.wall_s(), "seq": frame["seq"], "rss_kib": rss,
              "state": frame["threads"][0]["state"] if frame["threads"] else None,
              "selected": frame["selected_thread_id"],
              "source_connected": frame["source"]["connected"]}
        self.checkpoints.append(cp)
        result["checkpoints"].append(cp)
        return frame

    def episode(self, result: dict, eid: str, title: str, fault: str) -> None:
        result["episodes"].append({"id": eid, "title": title, "fault": fault,
                                   "wall_s": self.wall_s(),
                                   "applies_so_far": self.applied,
                                   "seq_so_far": self.engine.seq})

    async def send_telemetry(self, mono: int, battery_mv: Optional[int]) -> None:
        """电池 trace 独立上行（不进入 State；sent_at_ms 携带虚拟时间）。"""
        payload = json.dumps({
            "schema_version": 1, "kind": "telemetry",
            "battery_mv": battery_mv, "battery_valid": battery_mv is not None,
            "rssi_dbm": -55, "transport": "wifi",
            "sent_at_ms": ANCHOR_MS + mono,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status = await self.client.send_telemetry(payload)
        if status.value == "accepted":
            self.tel_sent.append(payload)
        else:
            self.tel_missed.append({"mono": mono, "status": status.value,
                                    "battery_mv": battery_mv})


def thread_of(frame: dict, tid: str) -> dict:
    t = next((x for x in frame["threads"] if x["id"] == tid), None)
    assert t is not None, f"帧中无线程 {tid}: {[x['id'] for x in frame['threads']]}"
    return t


def check_maker(result: dict) -> Callable[[str, bool, str], None]:
    def check(name: str, cond: bool, detail: str = "") -> None:
        result["assertions"].append({"name": name, "pass": bool(cond),
                                     "detail": detail})
        assert cond, f"R6 断言失败: {name} {detail}"
    return check


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


async def run_timeline(o: Orchestrator, result: dict) -> None:
    check = check_maker(result)

    # ---- E0 启动：同一组生产对象 + 首份全量 ------------------------------
    o.episode(result, "E0", "启动同一组生产对象（engine/server/订户各一）", "无")
    await o.start_all()
    await wait_for(lambda: o.client.link_state is LinkState.CONNECTED,
                   what="订户首次 CONNECTED")
    snap = await o.publish(ev.rate_limits((
        {"id": "codex-primary", "label": "300 MIN WINDOW", "used_percent": 42.0,
         "duration_mins": 300, "resets_at_ms": ANCHOR_MS + 3600000},),
        at_ms=100), mono=100)
    assert snap["seq"] == 1 and o.engine.seq == 1
    await o.checkpoint(result, "E0", "baseline", 100, expect={
        "usage.available": True,
        "usage.windows.0.used_percent": 42.0,
        "threads_total": 0,
    })
    assert o.rss_series, "RSS 基线缺失（psutil 不可用？）"

    # ---- E1 多 thread/turn 生命周期 --------------------------------------
    a = "thread-alpha"
    o.episode(result, "E1", "生命周期 WORKING→NEEDS YOU→等待解除→DONE", "无")
    m = T_E1
    await o.publish(ev.thread_started(a, "codex-desk-terminal", at_ms=m), m)
    await o.publish(ev.turn_started(a, "turn-001", summary="重构状态同步",
                                    at_ms=m + 500), m + 500)
    await o.publish(ev.token_usage(a, 154000, 258400, turn_id="turn-001",
                                   at_ms=m + 1000), m + 1000)
    await o.publish(ev.plan_updated(a, "turn-001", (
        ("检查需求", ev.PLAN_COMPLETED), ("实现界面", ev.PLAN_IN_PROGRESS),
        ("运行测试", ev.PLAN_PENDING)), total=3, at_ms=m + 1500), m + 1500)
    await o.publish(ev.item_started(a, "turn-001", "commandExecution",
                                    item_id="item-exec-1", summary="pytest -q tests",
                                    at_ms=m + 2000), m + 2000)
    await o.publish(ev.approval_requested(a, 0, "运行命令需要批准",
                                          turn_id="turn-001", at_ms=m + 2500), m + 2500)
    frame = await o.checkpoint(result, "E1", "needs_you", m + 2500, expect={
        "threads.0.state": "needs_you",
        "threads.0.attention.pending_count": 1,
        "threads.0.attention.summary": "运行命令需要批准",
        "selected_thread_id": a,
    })
    # 等待保持：后续内容事件（PLAN UPDATE 保持 NEEDS YOU）推进 waiting_ms
    await o.publish(ev.plan_updated(a, "turn-001", (
        ("检查需求", ev.PLAN_COMPLETED), ("实现界面", ev.PLAN_IN_PROGRESS)), total=3,
        at_ms=m + 2600), m + 2600)
    frame = await o.checkpoint(result, "E1", "needs_you_hold", m + 2600, expect={
        "threads.0.state": "needs_you",
        "threads.0.attention.pending_count": 1,
    })
    check("E1 waiting_ms 随虚拟时钟推进", frame["threads"][0]["waiting_ms"] >= 100)
    await o.publish(ev.server_request_resolved(a, 0, at_ms=m + 3000), m + 3000)
    await o.checkpoint(result, "E1", "resolved_back_to_working", m + 3000, expect={
        "threads.0.state": "working", "threads.0.attention": None,
    })
    await o.publish(ev.item_completed(a, "turn-001", "commandExecution",
                                      item_id="item-exec-1", at_ms=m + 3500), m + 3500)
    await o.publish(ev.turn_completed(a, "turn-001", ev.TURN_STATUS_COMPLETED,
                                      at_ms=m + 4000), m + 4000)
    await o.checkpoint(result, "E1", "done", m + 4000, expect={
        "threads.0.state": "done", "threads.0.end_reason": "completed",
        "threads.0.turn_id": "turn-001",
        "threads.0.context.used_tokens": 154000,
    })

    # ---- E2 重复 seq 洪泛 + 乱序 -----------------------------------------
    o.episode(result, "E2", "重复 seq 洪泛 ×40（server 去重）+ 乱序旧 seq ×3",
              "重复 seq 洪泛 / 乱序")
    m = T_E2
    flood_blob = o.history[-1][1]
    dedup_before = o.server.stats.snapshots_deduped
    for _ in range(40):
        await o.resend(flood_blob)
    await o.quiesce()
    check("E2 洪泛 ×40 全部去重（订户零新增帧）",
          o.server.stats.snapshots_deduped - dedup_before == 40
          and o.model.dedup_expected == 40)
    # 乱序注入用 3 份**互不相同**的历史快照（server 按连接对相同字节去重，
    # 相同 blob 连发只会送达第一份；三份不同旧 seq 帧均应被推送并被对账器计 stale）
    stale_blobs = [raw for mono, raw in o.history if mono in (100, 2500, 3600)]
    assert len(stale_blobs) == 3
    for blob in stale_blobs:
        await o.resend(blob)
    await o.quiesce()
    check("E2 乱序帧 == 注入数 3 且不改最新应用帧",
          len(o.rec.stale) == 3 and o.rec.latest["seq"] == o.engine.seq
          and all(s[0] == EPOCH for s in o.rec.stale))
    await o.publish(ev.select_thread(a, at_ms=m + 100), m + 100)
    await o.checkpoint(result, "E2", "post_flood_converged", m + 100, expect={
        "selected_thread_id": a,
    })

    # ---- E3 桥重启 ×3（同一 WssServer 对象同端口 stop/start；engine 不重建）
    for i, base in enumerate(T_E3, start=1):
        o.episode(result, f"E3.{i}", f"桥重启 {i}/3（同端口，engine 不重建）",
                  "桥重启（close 1000 → 退避重连）")
        m = base
        await o.publish(ev.token_usage(a, 160000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m), m)
        await o.quiesce()
        await o.server.stop()
        o.server_up = False
        assert o.server.client_count == 0
        # 先等订户确认断链（close 异步送达；link_state 不会瞬间翻转，直接等
        # CONNECTED 会读到停机前的陈旧状态）
        await wait_for(lambda: o.client.link_state is LinkState.DISCONNECTED,
                       what=f"重启{i}前订户确认 DISCONNECTED（close 1000 送达）")
        # server down 期间编排照常 apply：BUSY 停靠（生产等价=停机窗口不达线路）
        await o.publish(ev.token_usage(a, 161000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m + 100), m + 100)
        await o.publish(ev.token_usage(a, 162000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m + 200), m + 200)
        await o.server.start()
        o.server_up = True
        await wait_for(lambda: o.client.link_state is LinkState.CONNECTED,
                       what=f"重启{i}后订户退避重连 CONNECTED")
        await o.quiesce()
        check(f"E3.{i} 重连取得停靠前最新全量（seq 连续、epoch 恒定）",
              o.rec.frames[-1] == o.model.expected[-1]
              and o.rec.latest["seq"] == o.engine.seq - 2
              and o.engine.bridge_epoch == EPOCH)
        await o.publish(ev.token_usage(a, 163000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m + 1000), m + 1000)
        await o.checkpoint(result, f"E3.{i}", "restarted_converged", m + 1000)

    # ---- E4 断连退避重连 ×5（TCP abort = 网络失联 1006 语义） --------------
    for i, base in enumerate(T_E4, start=1):
        o.episode(result, f"E4.{i}", f"断连退避重连 {i}/5", "TCP abort（1006 类）")
        m = base
        await o.publish(ev.token_usage(a, 170000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m), m)
        await o.quiesce()
        conns = list(o.server._conns)  # 故障注入：拔网线等价（docstring 边界 4）
        assert conns, "无已连接可注入断连"
        for c in conns:
            c.transport.abort()
        await wait_for(lambda: o.client.link_state is LinkState.DISCONNECTED,
                       what=f"abort{i} 后订户进入 DISCONNECTED")
        await wait_for(lambda: o.client.link_state is LinkState.CONNECTED,
                       what=f"abort{i} 后按退避重连 CONNECTED")
        await o.quiesce()
        check(f"E4.{i} 重连后取得最新全量",
              o.rec.frames[-1] == o.model.expected[-1])
        await o.publish(ev.token_usage(a, 171000 + i * 1000, 258400,
                                       turn_id="turn-001", at_ms=m + 1000), m + 1000)
        await o.checkpoint(result, f"E4.{i}", "reconnected_converged", m + 1000)

    # ---- E5 跨任务多线程 + 审批 pending→resolve + usage --------------------
    o.episode(result, "E5", "审批×2 部分解除、新 turn 不继承 DONE、迟到门闸、额度更新",
              "无（业务编排）")
    b, g = "thread-beta", "thread-gamma"
    m = T_E5
    await o.publish(ev.turn_started(a, "turn-002", summary="第二轮修复", at_ms=m), m)
    await o.checkpoint(result, "E5", "new_turn_not_inherit_done", m, expect={
        "threads.0.id": a, "threads.0.state": "working",
        "threads.0.end_reason": None, "threads.0.plan.total": 0,
    })
    await o.publish(ev.thread_started(b, "proj-beta", at_ms=m + 100), m + 100)
    await o.publish(ev.thread_started(g, "proj-gamma", at_ms=m + 200), m + 200)
    await o.publish(ev.turn_started(b, "turn-b1", summary="更新文档",
                                    at_ms=m + 300), m + 300)
    await o.publish(ev.turn_started(g, "turn-g1", summary="迁移数据库",
                                    at_ms=m + 400), m + 400)
    await o.publish(ev.approval_requested(b, 100, "确认部署目标",
                                          turn_id="turn-b1", at_ms=m + 500), m + 500)
    await o.publish(ev.approval_requested(b, 101, "确认删除分支",
                                          turn_id="turn-b1", at_ms=m + 600), m + 600)
    await o.checkpoint(result, "E5", "pending_x2", m + 600, expect={
        "threads.0.id": b, "threads.0.state": "needs_you",
        "threads.0.attention.pending_count": 2,
    })
    await o.publish(ev.server_request_resolved(b, 100, at_ms=m + 700), m + 700)
    await o.checkpoint(result, "E5", "partial_resolve_still_needs_you", m + 700,
                       expect={
                           "threads.0.state": "needs_you",
                           "threads.0.attention.pending_count": 1,
                       })
    await o.publish(ev.server_request_resolved(b, 101, at_ms=m + 800), m + 800)
    await o.checkpoint(result, "E5", "fully_resolved_working", m + 800, expect={
        "threads.0.state": "working", "threads.0.attention": None,
    })
    await o.publish(ev.rate_limits((
        {"id": "codex-primary", "label": "300 MIN WINDOW", "used_percent": 58.0,
         "duration_mins": 300, "resets_at_ms": ANCHOR_MS + 7200000},
        {"id": "codex-secondary", "label": "10080 MIN WINDOW", "used_percent": 31.0,
         "duration_mins": 10080, "resets_at_ms": ANCHOR_MS + 86400000},
    ), at_ms=m + 900), m + 900)
    await o.publish(ev.token_usage(b, 90000, 200000, turn_id="turn-b1",
                                   at_ms=m + 950), m + 950)
    await o.checkpoint(result, "E5", "usage_updated", m + 950, expect={
        "usage.available": True, "usage.windows_total": 2,
        "usage.windows.0.used_percent": 58.0,
        "usage.windows.1.used_percent": 31.0,
        "threads.0.context.used_tokens": 90000,
        "threads.0.context.used_percent": 45.0,
    })
    # 迟到事件门闸：已终态 turn-001 的事件被丢弃（状态不变、seq 照常推进）
    await o.publish(ev.approval_requested(a, 999, "迟到的批准请求",
                                          turn_id="turn-001", at_ms=m + 1000), m + 1000)
    frame = await o.checkpoint(result, "E5", "late_event_gated", m + 1000)
    ta = thread_of(frame, a)
    check("E5 迟到事件不复活已终态 turn",
          ta["state"] == "working" and ta["attention"] is None
          and frame["seq"] == o.engine.seq)
    await o.publish(ev.thread_started(b, "proj-beta-renamed", at_ms=m + 1100), m + 1100)
    frame = await o.checkpoint(result, "E5", "dup_thread_started", m + 1100)
    check("E5 重复 thread_started 不改已存在线程状态",
          thread_of(frame, b)["state"] == "working")

    # ---- E6 上游断连/重连（source 标记，任务态不被伪造） -------------------
    o.episode(result, "E6", "上游 source 断连→重连",
              "source_disconnected / source_reconnected")
    m = T_E6
    await o.publish(ev.source_disconnected(at_ms=m), m)
    frame = await o.checkpoint(result, "E6", "source_disconnected", m, expect={
        "source.connected": False, "source.stale": True,
    })
    check("E6 断连不把任务态改 IDLE / 不伪造 DONE",
          thread_of(frame, a)["state"] == "working"
          and thread_of(frame, b)["state"] == "working")
    await o.publish(ev.source_reconnected(at_ms=m + 10000), m + 10000)
    await o.checkpoint(result, "E6", "source_reconnected", m + 10000, expect={
        "source.connected": True, "source.stale": False,
    })

    # ---- E7 电池 trace 独立驱动（S09 类，独立于快照时间线） -----------------
    o.episode(result, "E7", "电池 trace 独立上行（S09 类 3980→3600，临界段 1 Hz）",
              "独立遥测通道（不进入 State）")
    m = T_E7
    trace = [3980, 3720, 3700, 3650, 3601] + [3600] * 55  # 60 样本，1 Hz 虚拟
    for i, mv in enumerate(trace):
        await o.send_telemetry(m + i * 1000, mv)
    await wait_for(lambda: len(o.tel_received) == len(o.tel_sent),
                   what=f"server 收满上行遥测 {len(o.tel_sent)} 条"
                        f"（当前 {len(o.tel_received)}）")
    check("E7 上行遥测逐条送达且字节一致",
          o.tel_received == o.tel_sent
          and o.server.stats.uplink_messages == len(o.tel_sent)
          and o.server.stats.uplink_bytes == sum(len(p) for p in o.tel_sent))
    tel_schema_ok = True
    tel_schema_err = ""
    for p in o.tel_sent:
        try:
            validate_tel(p, "E7")
        except AssertionError as exc:
            tel_schema_ok = False
            tel_schema_err = str(exc)[:200]
            break
    check("E7 遥测全部 ≤512 B 且过 telemetry schema",
          all(len(p) <= 512 for p in o.tel_sent) and tel_schema_ok, tel_schema_err)
    parsed = [json.loads(p.decode("utf-8")) for p in o.tel_sent]
    critical = [(d["sent_at_ms"], d["battery_mv"]) for d in parsed
                if d["battery_mv"] is not None and d["battery_mv"] <= 3700]
    gaps = [q[0] - p2[0] for p2, q in zip(critical, critical[1:])]
    check("E7 临界段 1 Hz 虚拟连续（间隔 ≤2000 ms）",
          len(critical) >= 2 and max(gaps) <= 2000,
          f"critical_n={len(critical)} max_gap={max(gaps) if gaps else 0}")
    check("E7 遥测不进入 State（下行快照无电压字段）",
          b"battery" not in o.rec.frames[-1] and not o.tel_missed)
    await o.checkpoint(result, "E7", "state_unaffected_by_telemetry", m + 59000)

    # ---- E8 最终收敛 -------------------------------------------------------
    o.episode(result, "E8", "最终收敛（全部 turn 终态、pending 清空）", "无")
    m = T_E8
    b_, g_ = "thread-beta", "thread-gamma"
    await o.publish(ev.turn_completed(a, "turn-002", ev.TURN_STATUS_COMPLETED,
                                      at_ms=m), m)
    await o.publish(ev.turn_completed(b_, "turn-b1", ev.TURN_STATUS_COMPLETED,
                                      at_ms=m + 100), m + 100)
    await o.publish(ev.turn_completed(g_, "turn-g1", ev.TURN_STATUS_FAILED,
                                      summary="编译错误：类型不匹配",
                                      at_ms=m + 200), m + 200)
    frame = await o.checkpoint(result, "E8", "final_converged", m + 200)
    check("E8 终态字段断言（alpha/beta done、gamma error 且排序居首）",
          thread_of(frame, a)["state"] == "done"
          and thread_of(frame, b_)["state"] == "done"
          and thread_of(frame, g_)["state"] == "error"
          and frame["threads"][0]["id"] == g_  # error 优先级高于 done（§3 排序）
          and frame["threads_total"] == 3)
    check("E8 本地选中跨全部故障保持（E2 选中 alpha 不被抢走）",
          frame["selected_thread_id"] == a)
    check("E8 gamma end_reason/activity/attention 正确",
          thread_of(frame, g_)["end_reason"] == "failed"
          and thread_of(frame, g_)["activity"] == "编译错误：类型不匹配"
          and all(t["attention"] is None for t in frame["threads"]))

    await o.quiesce()


# ---------------------------------------------------------------------------
# 收尾全局断言（对账 / 有界性 / 内存）
# ---------------------------------------------------------------------------


def final_assertions(o: Orchestrator, result: dict, check) -> dict:
    # 内存判据：复用 soak_bridge.mem_verdict 口径（importlib 加载同一实现）
    mem: dict = {}
    if o.rss_series and len(o.rss_series) >= 2:
        mem["sampled"] = o.soak.mem_verdict(o.rss_series, mode="sampled")
        mem["series_kib"] = [round(v, 1) for v in o.rss_series]
    import resource
    mem["highwater_last_kib"] = round(o.soak.rss_kib_of_rusage(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), 1)
    mem["note"] = ("ru_maxrss 单调不减（高水位参照值）；判定用检查点 RSS 序列，"
                   "判据与 soak_bridge.py 同口径：|末-首| ≤ max(2048 KiB, 25%×首)，"
                   "斜率仅展示不判定")

    cs, ss = o.client.stats, o.server.stats
    recon = {
        "model_expected_frames": len(o.model.expected),
        "received_frames": len(o.rec.frames),
        "frames_equal_model": o.rec.frames == o.model.expected,
        "received_bytes": cs.bytes_received,
        "expected_bytes": sum(len(f) for f in o.model.expected),
        "server_snapshots_pushed": ss.snapshots_pushed,
        "client_messages_received": cs.messages_received,
        "deduped": ss.snapshots_deduped,
        "dedup_expected": o.model.dedup_expected,
        "stale_reordered": len(o.rec.stale),
        "stale_detail": [list(s) for s in o.rec.stale],
        "redeliveries": len(o.rec.redeliveries),
        "parked_busy_publishes": o.model.parked,
        "produced_blobs": len(o.produced),
        "received_subset_of_produced": set(o.rec.frames).issubset(o.produced),
        "uplink": {"sent": len(o.tel_sent), "received": len(o.tel_received),
                   "sent_bytes": sum(len(p) for p in o.tel_sent),
                   "server_uplink_bytes": ss.uplink_bytes,
                   "missed": o.tel_missed},
    }

    check("对账：订户收帧列表逐字节等于模型期望（含重连全量帧，无静默丢帧）",
          recon["frames_equal_model"])
    check("对账：server 推帧数 == 订户收帧数 == 模型帧数",
          ss.snapshots_pushed == cs.messages_received == len(o.model.expected))
    check("对账：收发字节数一致", recon["received_bytes"] == recon["expected_bytes"])
    check("对账：无凭空造帧（收到帧 ⊆ engine 产出集）",
          recon["received_subset_of_produced"])
    check("对账：去重计数一致（洪泛 ×40）",
          recon["deduped"] == recon["dedup_expected"] == 40)
    check("对账：乱序帧有界（== 注入数 3，epoch 恒定）",
          recon["stale_reordered"] == 3)
    check("对账：重连全量重投有界（== 8 次 = 3 重启 + 5 断连，seq == max 合法重投）",
          recon["redeliveries"] == 8,
          f"redeliveries={recon['redeliveries']}")
    check("对账：BUSY 停靠计数一致（3 次重启 × 2 帧 = 6）",
          recon["parked_busy_publishes"] == len(o.busy_parked_events) == 6)
    check("对账：上行遥测无丢失（逐条 + 字节一致）",
          o.tel_received == o.tel_sent
          and ss.uplink_messages == len(o.tel_sent)
          and ss.uplink_bytes == sum(len(p) for p in o.tel_sent))

    # seq/epoch 无回退
    check("seq 无回退：订户最新应用帧 seq == engine.seq",
          o.rec.latest["seq"] == o.engine.seq)
    check("epoch 恒定：全程单一 epoch（engine 未重建）",
          set(o.rec.epoch_max) == {EPOCH} and o.engine.bridge_epoch == EPOCH)
    seqs = [json.loads(f.decode("utf-8"))["seq"] for f in o.rec.frames]
    max_so_far, violations = 0, 0
    for s in seqs:
        if s < max_so_far:
            violations += 1
        max_so_far = max(max_so_far, s)
    check("线路序中 seq 回退仅出现在注入的 3 帧乱序", violations == 3,
          f"violations={violations}")

    # engine 内部计数有界
    internal = o.engine.internal
    check("engine 内部：threads 数 == 注入去重数（无幽灵增长）",
          len(internal["threads"]) == 3)
    check("engine 内部：全部 pending 清空（无累积悬挂）",
          all(not rec.pending for rec in internal["threads"].values()))
    check("engine 内部：seq == apply 次数（无跳号/重号）",
          o.engine.seq == o.applied == len(o.history))
    check("engine 内部：usage 收敛于最后更新",
          internal["usage"]["available"] is True
          and len(internal["usage"]["windows"]) == 2)

    # 缺陷计数全零
    check("server 缺陷计数全零（oversize/non_utf8/401/403/404）",
          ss.oversize_out_rejected == 0 and ss.non_utf8_out_rejected == 0
          and ss.rejected_auth_401 == 0 and ss.rejected_policy_403 == 0
          and ss.rejected_path_404 == 0)
    check("订户缺陷计数全零（config_error/oversize/CONFIG_ERROR 后重试）",
          cs.config_errors == 0 and cs.oversize_rejected == 0
          and cs.retries_after_config_error == 0)
    check("订户最终仍 CONNECTED（同一对象经历全部故障后存活）",
          o.client.link_state is LinkState.CONNECTED)
    check("连接账目：连接 9 次（1 首连 + 3 重启 + 5 断连）",
          cs.connected_count == 9, f"connected_count={cs.connected_count}")
    check("close 账目：恰好 3×1000（重启）+ ≥5×None（abort），无 1008/1009",
          cs.close_codes.count(1000) == 3
          and sum(1 for c in cs.close_codes if c is None) >= 5
          and 1008 not in cs.close_codes and 1009 not in cs.close_codes,
          f"close_codes={cs.close_codes}")
    tier_ok = all(
        0.8 * TEST_BACKOFF.sequence_s[min(i, len(TEST_BACKOFF.sequence_s) - 1)]
        <= d <= 1.2 * TEST_BACKOFF.sequence_s[min(i, len(TEST_BACKOFF.sequence_s) - 1)]
        for i, d in enumerate(cs.retry_delays_s))
    check("退避档位按失败序号在 ±20% 带内推进（无跳档/回退）",
          tier_ok and len(cs.retry_delays_s) == len(cs.close_codes) == 8,
          f"delays={[round(d, 3) for d in cs.retry_delays_s]} "
          f"close_codes={cs.close_codes} attempts={cs.attempts} "
          f"connected={cs.connected_count}")
    check("server 链路账目：connected 事件 == 订户连接数",
          sum(1 for s, _ in o.server_link_log if s == "connected")
          == cs.connected_count)
    check("虚拟时间线 ≥10 分钟等效", VIRTUAL_SPAN_MS >= MIN_VIRTUAL_SPAN_MS,
          f"{VIRTUAL_SPAN_MS} ms")
    if mem.get("sampled"):
        check("内存判据（soak 口径：|末-首| ≤ max(2048 KiB, 25%×首)）",
              mem["sampled"]["pass"] is True,
              json.dumps(mem["sampled"], ensure_ascii=False))

    return {"memory": mem, "reconciliation": recon,
            "server_stats": vars(ss),
            "client_stats": {k: (list(v) if isinstance(v, list) else v)
                             for k, v in vars(cs).items()},
            "retry_delays_s": [round(d, 4) for d in cs.retry_delays_s],
            "busy_parked_events": o.busy_parked_events,
            "virtual_span_ms": VIRTUAL_SPAN_MS,
            "rss_series_kib": [round(v, 1) for v in o.rss_series]}


async def shutdown(o: Orchestrator) -> None:
    await o.client.stop()
    if o.client_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(o.client_task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            o.client_task.cancel()
    await o.server.stop()
    o.server_up = False


# ---------------------------------------------------------------------------
# 证据落盘
# ---------------------------------------------------------------------------


def write_artifacts(result: dict, error: Optional[str]) -> None:
    ART.mkdir(parents=True, exist_ok=True)
    result["meta"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    result["meta"]["error"] = error
    result["overall_pass"] = error is None and all(
        a["pass"] for a in result["assertions"])
    (ART / "timeline.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    (ART / "report.md").write_text(render_md(result), encoding="utf-8")


def render_md(r: dict) -> str:
    m = r["meta"]
    lines: list[str] = []
    a = lines.append
    a("# continuous-report — R6 同实例持续编排测试（A5）")
    a("")
    a(f"- 日期：{m.get('finished')}；结果：{m.get('error') or '全部断言通过'}")
    a(f"- 复现命令：`sh scripts/run_continuous_test.sh`（真实退出码；本轮输出 "
      f"`artifacts/continuous/run_log.txt`；时间线 JSON "
      f"`artifacts/continuous/timeline.json`）")
    a(f"- 宿主：{m['platform']}；Python {m['python']}")
    a(f"- 虚拟时间线跨度：{m['virtual_span_ms']} ms（≥10 分钟等效）；真实墙钟 "
      f"{m['wall_s']} s（时钟压缩 ≈"
      f"{m['virtual_span_ms'] / 1000.0 / max(m['wall_s'], 0.001):.1f}×）；"
      f"端口 {m['port']}（临时预留，未用 8765）")
    a("")
    a("## 边界声明")
    a("")
    a("- 本测试只覆盖 **Bridge 侧同实例编排**（PC 级）；**固件 main 的同实例持续"
      "验证归 24h 真机 soak（P6.2 剩余项）**，不在此假装覆盖无线/电源/LCD/ADC。")
    a("- 与 `scripts/soak_bridge.py` **互补不替代**：soak = 确定性回归（每轮新 "
      "engine/新进程）；本测试 = 同一 engine/server/订户连续经历故障编排。")
    a("- 桥重启用**同一 WssServer 对象**同端口 stop/start，engine 刻意不重建：断言 "
      "epoch 恒定、seq 跨重启严格递增；S21「新 epoch 重置」语义由对账器自检"
      "（合成帧）覆盖。")
    a("- 模拟器长驻进程为任务可选项，未包含（UI soak 归 P6.2）。keepalive 存活"
      "快照置 None（周期推送语义归 P3.3/P3.5），保证对账静止点确定性。")
    a("")
    a("## 时间线覆盖矩阵")
    a("")
    a("| 集段 | 注入/覆盖 | 累计 apply（集段起） | 累计 seq（集段起） | 墙钟 s |")
    a("|---|---|---|---|---|")
    for e in r["episodes"]:
        a(f"| {e['id']} {e['title']} | {e['fault']} | {e['applies_so_far']} | "
          f"{e['seq_so_far']} | {e['wall_s']} |")
    a("")
    a("## 对账结论")
    a("")
    fin = r.get("final") or {}
    rc = fin.get("reconciliation", {})
    if rc:
        up = rc.get("uplink", {})
        a(f"- 期望帧 {rc.get('model_expected_frames')} == 收到帧 "
          f"{rc.get('received_frames')}，逐字节按序相等："
          f"{rc.get('frames_equal_model')}；server 推帧 "
          f"{rc.get('server_snapshots_pushed')} == 订户收帧 "
          f"{rc.get('client_messages_received')}；字节 "
          f"{rc.get('received_bytes')}/{rc.get('expected_bytes')}")
        a(f"- 去重 {rc.get('deduped')}（期望 40）；乱序 {rc.get('stale_reordered')}"
          f"（注入 3，有界）；BUSY 停靠 {rc.get('parked_busy_publishes')}"
          f"（3 次重启 × 2）；造帧检查（收到 ⊆ 产出）："
          f"{rc.get('received_subset_of_produced')}")
        a(f"- 上行遥测：发 {up.get('sent')} 收 {up.get('received')}，字节 "
          f"{up.get('sent_bytes')}/{up.get('server_uplink_bytes')}，未达 "
          f"{len(up.get('missed', []))} 条")
    else:
        a("- （中途失败，对账汇总未生成；见 timeline.json 的 final_partial）")
    a("")
    a("## 内存（soak 口径）")
    a("")
    mem = fin.get("memory", {})
    sm = mem.get("sampled")
    if sm:
        a(f"- 检查点 RSS 序列（{sm['mode']}，{len(mem.get('series_kib', []))} 点）："
          f"首 {sm['first_kib']} KiB → 末 {sm['last_kib']} KiB，最小 "
          f"{sm['min_kib']} / 峰值 {sm['peak_kib']} KiB，漂移 "
          f"{sm['measured_last_first_kib']} KiB，斜率（仅展示）"
          f"{sm['slope_kib_per_round']} KiB/点 → "
          f"{'PASS' if sm['pass'] else 'FAIL'}")
    a(f"- ru_maxrss 末值 {mem.get('highwater_last_kib')} KiB（单调高水位，参照值）")
    a("")
    a("## 断言明细")
    a("")
    a("| 断言 | 结果 |")
    a("|---|---|")
    for x in r["assertions"]:
        mark = "PASS" if x["pass"] else "FAIL"
        tail = f"（{x['detail'][:140]}）" if (x["detail"] and not x["pass"]) else ""
        a(f"| {x['name']} | {mark}{tail} |")
    a("")
    a("## 剩余问题（不在本测试范围）")
    a("")
    a("- 固件 main 同实例持续验证、24h 真机、100 次重连、主机睡眠唤醒：归 P6.2 "
      "真机段。")
    a("- keepalive 存活快照、TLS/wss、BLE：由 P3.3/P3.5 各自覆盖；本测试为明文 "
      "ws loopback。")
    a("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pytest 入口
# ---------------------------------------------------------------------------


async def _amain(o: Orchestrator, result: dict) -> None:
    try:
        await run_timeline(o, result)
        result["final"] = final_assertions(o, result, check_maker(result))
    finally:
        await shutdown(o)


def test_continuous_orchestration_same_instance() -> None:
    """R6：同一生产编排对象（engine+server+订户）连续经历故障时间线后仍收敛。

    边界声明见模块 docstring（Bridge 侧；固件 main 归 P6.2 24h 真机 soak；
    与 soak_bridge.py 互补不替代）。
    """
    result: dict = {
        "meta": {
            "task": "R6 同实例持续编排测试（PC 级，Bridge 侧）",
            "review_source": "docs/ARCHITECTURE_REVIEW_2026-09-11.md R6 / P2",
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "epoch": EPOCH,
            "port": None,
            "virtual_span_ms": VIRTUAL_SPAN_MS,
            "wall_s": None,
            "boundary": [
                "Bridge 侧同实例编排；固件 main 同实例持续验证归 P6.2 24h 真机 soak",
                "与 soak_bridge.py 互补不替代：soak=确定性回归；本测试=同实例故障编排",
                "模拟器长驻进程为可选项，未包含（UI soak 归 P6.2）",
            ],
        },
        "episodes": [],
        "checkpoints": [],
        "assertions": [],
    }
    error: Optional[str] = None
    o: Optional[Orchestrator] = None
    t0 = time.monotonic()
    try:
        o = Orchestrator(result)
        result["meta"]["port"] = o.port
        Reconciler().self_check()
        result["assertions"].append(
            {"name": "对账器自检（epoch 交替/stale 判定，合成帧）", "pass": True,
             "detail": ""})
        asyncio.run(_amain(o, result))
    except BaseException as exc:  # noqa: BLE001 — 失败也要落证据再上抛
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["meta"]["wall_s"] = round(time.monotonic() - t0, 2)
        if o is not None and "final" not in result:
            try:  # 中途失败：尽量补齐可得统计证据
                result["final_partial"] = {
                    "server_stats": vars(o.server.stats),
                    "client_close_codes": list(o.client.stats.close_codes),
                    "received_frames": len(o.rec.frames),
                    "expected_frames": len(o.model.expected),
                }
            except Exception:  # noqa: BLE001
                pass
        write_artifacts(result, error)


if __name__ == "__main__":  # 直跑：python tests/integration/test_continuous_orchestration.py
    test_continuous_orchestration_same_instance()
    print("R6 continuous orchestration: PASS")
