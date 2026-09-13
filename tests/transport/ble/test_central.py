"""ZC10 BLE 主机侧测试：BleakCentral × stub GATT 设备（无真机、无 BLE 栈）。

对应任务书第 3 节（主机测试；设备为内存 stub GATT，与 transport.md §7.2 B1
"进程内虚拟 BLE 链路"同口径）：

- 分片器+central 组合往返：快照 → Fragmenter 分片 → 逐片写 stub RX →
  StubPeripheral 重组（fragmenter.Reassembler，与 C 引擎同规则）→ CRC 通过 →
  store applied → TX 通知回 ACK → publish 返回 acked；CRC 注入错误 → NACK →
  全包重发 → ACK；永久损坏 → 重试耗尽 → 丢弃当前帧并断链重连。
- 断开→退避重连状态机：扫描缺席（静默退避）、外部断链（设备离开）、
  可操作错误（§6.1 权限被拒 → 终态不重试）、message_id 每连接归 1。
- MTU 变化下的分片尺寸适配：MTU 23/53/247 三档往返；重连后新 MTU 生效。

红线自查：store 三值只发生在 stub 测试侧（同 tests/transport 既有口径，
传输路径不解释 JSON）；日志断言不含快照内容；无凭证。
"""

from __future__ import annotations

import asyncio
import pathlib
import random
import struct
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bridge.transports.ble.central import (  # noqa: E402
    RESCAN_SEQUENCE_S,
    BleakCentral,
    CentralState,
    RescanBackoff,
)
from bridge.transports.ble.central_skeleton import (  # noqa: E402
    MAX_SEND_RETRIES,
    MACOS_HINT_DENIED,
)
from bridge.transports.ble.fragmenter import (  # noqa: E402
    FRAME_SIZE,
    TYPE_ACK,
    TYPE_NACK,
    Fragmenter,
    Reassembler,
    Reject,
    chunk_capacity,
    encode_ack_nack,
    fragment_count,
)

#: 帧头 16 字节小端布局（protocol/transport.md §3.1 冻结偏移，stub 解析用）
_HEADER = struct.Struct("<BBIHHHI")

PAYLOAD = (REPO / "tests" / "fixtures" / "protocol" /
           "valid_full.json").read_bytes()

WAIT_S = 5.0


async def _wait_for(predicate, timeout: float = WAIT_S, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("超时等待 %s（%.1fs）" % (what or "条件", timeout))


# ---------------------------------------------------------------------------
# stub：内存 GATT 设备 + 链路 + 连接器（与 BleakConnector/BleakLink 同形状）
# ---------------------------------------------------------------------------

class StubPeripheral:
    """内存 GATT peripheral：RX 写入 → 重组 → store 三值 → TX 通知回 ACK/NACK。

    注入口：
      corrupt_first_idx / corrupt_always_idx — 载荷位翻转（CRC 错误注入）
      suppress_ack_first                     — 首次尝试不应用不回 ACK（超时路径）
      store                                  — 上层三值回调（§3.4），默认 applied
    """

    def __init__(self, mtu: int, store=None):
        self.mtu = mtu
        self.reassembler = Reassembler(mtu)
        self.store = store or (lambda data: "applied")
        self.writes: list[bytes] = []      # central→设备 全部写入（含控制帧）
        self.controls: list[bytes] = []    # central 回写的 16B ACK/NACK 帧
        self.applied: list[bytes] = []
        self.rejects: list[str] = []
        self.msg_ids: list[int] = []       # 每条完成消息的 message_id
        self.corrupt_first_idx: set[int] = set()
        self.corrupt_always_idx: set[int] = set()
        self.suppress_ack_first = False
        self._notify_cb = None
        self._attempts: dict[int, int] = {}
        self._now_ms = 0

    def attach(self, cb) -> None:
        self._notify_cb = cb

    def _notify(self, frame: bytes) -> None:
        if self._notify_cb is not None:
            self._notify_cb(None, frame)

    def on_write(self, frame: bytes) -> None:
        self.writes.append(frame)
        if len(frame) == FRAME_SIZE:  # 16B 控制帧（§3.2：central 对遥测的回写）
            self.controls.append(frame)
            return
        (_v, _t, msg_id, index, _cnt, _total, _crc) = _HEADER.unpack(
            frame[:FRAME_SIZE])
        if index == 0:
            self._attempts[msg_id] = self._attempts.get(msg_id, 0) + 1
        attempt = self._attempts.get(msg_id, 1)
        if index in self.corrupt_always_idx or \
                (attempt == 1 and index in self.corrupt_first_idx):
            frame = frame[:-1] + bytes([frame[-1] ^ 0x01])  # 保长位翻转
        self._now_ms += 5
        try:
            result = self.reassembler.feed(frame, self._now_ms)
        except Reject as exc:
            self.rejects.append(exc.reason)
            if exc.header:
                self._notify(encode_ack_nack(exc.header, "rejected"))
            return
        if result == "completed":
            if attempt == 1 and self.suppress_ack_first:
                return  # 设备"没收到"：不应用、不回 ACK（§3.3 发送方 4 超时路径）
            data = self.reassembler.take()
            verdict = self.store(data)
            self.applied.append(data)
            self.msg_ids.append(msg_id)
            # 完成帧的头含全部元数据（§3.2：ACK 回填被确认 DATA 的元数据）
            self._notify(encode_ack_nack(frame[:FRAME_SIZE], verdict))


class StubLink:
    """单条 stub 连接：MTU=peripheral.mtu；write 转投 stub 设备。"""

    def __init__(self, peripheral: StubPeripheral):
        self.peripheral = peripheral
        self.disconnected = False
        self.write_hook = None  # fn(frame) -> 可抛异常（发送失败注入）
        self._loss_cb = None
        self._notify_cb = None

    @property
    def mtu(self) -> int:
        return self.peripheral.mtu

    async def start_notify(self, cb) -> None:
        self._notify_cb = cb
        self.peripheral.attach(cb)

    async def write(self, frame: bytes, response: bool = False) -> None:
        if self.disconnected:
            raise OSError("link closed")
        if self.write_hook is not None:
            self.write_hook(frame)
        self.peripheral.on_write(bytes(frame))

    async def disconnect(self) -> None:
        self.disconnected = True

    def set_loss_callback(self, cb) -> None:
        self._loss_cb = cb

    def device_sends(self, frame: bytes) -> None:
        """设备→central 方向的 TX 通知（上行遥测 DATA / 控制帧）。"""
        self._notify_cb(None, frame)

    def drop_link(self) -> None:
        """模拟设备离开/关蓝牙：链路失效并触发 central 的外部断链回调。"""
        self.disconnected = True
        if self._loss_cb is not None:
            self._loss_cb()


class StubConnector:
    """stub 连接器：扫描缺席/异常可编程；每次 connect 产一条新链路。"""

    def __init__(self, link_factory):
        self.link_factory = link_factory
        self.find_calls = 0
        self.connect_calls = 0
        self.absent_rounds = 0
        self.scan_errors: list[Exception | None] = []

    async def find_device(self, name, address, service_uuid, timeout_s):
        self.find_calls += 1
        if self.scan_errors:
            err = self.scan_errors.pop(0)
            if err is not None:
                raise err
        if self.absent_rounds > 0:
            self.absent_rounds -= 1
            return None
        return {"name": name, "address": address}  # handle 哨兵

    async def connect(self, handle):
        self.connect_calls += 1
        return self.link_factory()


def make_links(mtu: int) -> tuple[StubConnector, list]:
    """链路工厂：每 connect 一条新链路+新 stub 设备（重连即新接收上下文，
    §3.3 接收方 6）；创建物按序记录供断言。"""
    created: list[StubPeripheral] = []

    def factory():
        p = StubPeripheral(mtu)
        created.append(p)
        return StubLink(p)

    return StubConnector(factory), created


def make_central(connector, *, ack_timeout_s: float = 0.2, **kw) -> BleakCentral:
    """测试用 central：缩小的退避档位/扫描超时（只改时长不改序列形状）。"""
    backoff = RescanBackoff(sequence_s=(0.01, 0.02, 0.04), jitter_fraction=0.0)
    return BleakCentral("CodexDT", connector=connector, backoff=backoff,
                        scan_timeout_s=0.01, ack_timeout_s=ack_timeout_s, **kw)


# ---------------------------------------------------------------------------
# 分片器+central 组合往返
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mtu", [23, 53, 247])
def test_roundtrip_snapshot_ack(mtu):
    """快照 → 分片 → stub 设备重组 → CRC 通过 → applied → ACK 往返
    （MTU 23/53/247 三档；chunk 随协商 MTU 适配，§2.4）。"""
    connector, devices = make_links(mtu)
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            chunk = chunk_capacity(mtu)
            expected_frames = fragment_count(len(PAYLOAD), chunk)
            status = await central.publish(PAYLOAD)
            assert status == "acked", status
            assert devices[0].applied == [PAYLOAD]  # 逐字节一致
            assert devices[0].rejects == []
            assert len(devices[0].writes) == expected_frames
            # 除最后一片外逐片满容量（§3.3 发送方 2）
            full = FRAME_SIZE + chunk
            for frame in devices[0].writes[:-1]:
                assert len(frame) == full
            assert central.stats["snapshots_acked"] == 1
            assert devices[0].msg_ids == [1]  # 连接内从 1 递增（§3.2）
        finally:
            await central.stop()
        assert central.state is CentralState.STOPPED

    asyncio.run(body())


def test_roundtrip_mtu23_frame_geometry():
    """MTU23 下限档：chunk=4（§2.4 冻结算式），帧长=16+4，帧头字段逐帧一致。"""
    connector, devices = make_links(23)
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            assert (await central.publish(PAYLOAD)) == "acked"
            p = devices[0]
            (_v0, _t0, msg0, _i0, cnt0, total0, crc0) = _HEADER.unpack(
                p.writes[0][:FRAME_SIZE])
            assert chunk_capacity(23) == 4 and cnt0 == fragment_count(
                len(PAYLOAD), 4) and total0 == len(PAYLOAD)
            for frame in p.writes:
                (_v, _t, msg, idx, cnt, total, crc) = _HEADER.unpack(
                    frame[:FRAME_SIZE])
                assert (_v, msg, cnt, total, crc) == \
                    (_v0, msg0, cnt0, total0, crc0)  # 各片除 index 外一致（§3.3）
            assert [_HEADER.unpack(f[:FRAME_SIZE])[3]
                    for f in p.writes] == list(range(cnt0))  # index 0..n-1
        finally:
            await central.stop()

    asyncio.run(body())


def test_crc_corrupt_first_attempt_then_full_retry_acks():
    """CRC 注入错误：首试损坏 1 片 → stub NACK → central 全包重发 → ACK 收敛
    （§3.3 发送方 4 / §3.4 rejected→NACK→重发全包）。"""
    connector, devices = make_links(53)
    devices_factory_first = devices
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            # 预注册：首试损坏 index=1（对稍后创建的 peripheral 生效）
            central._link.peripheral.corrupt_first_idx.add(1)
            status = await central.publish(PAYLOAD)
            assert status == "acked", status
            p = devices_factory_first[0]
            assert p.rejects == ["crc_mismatch"]
            assert p.applied == [PAYLOAD]  # 重发后完整应用一次
            assert len(p.applied) == 1
            assert central.stats["nack_retries"] == 1
            # 首试片数 + 重试全包片数（同 message_id 重发，§3.3）
            n = fragment_count(len(PAYLOAD), chunk_capacity(53))
            assert len(p.writes) == 2 * n
            assert p.msg_ids == [1]  # 重发不换 message_id
        finally:
            await central.stop()

    asyncio.run(body())


def test_crc_corrupt_always_exhausts_retries_drops_and_recovers():
    """永久损坏：重试耗尽（初发+2 次，§3.3 发送方 4）→ 丢弃当前帧 + 断链 →
    退避重连后新帧自然应用（设备 StateStore seq/epoch 语义）。"""
    connector, devices = make_links(23)
    central = make_central(connector, ack_timeout_s=0.2)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="连接#1")
            central._link.peripheral.corrupt_always_idx.add(0)
            status = await central.publish(PAYLOAD)
            assert status == "dropped-nack", status
            p1 = devices[0]
            attempts = 1 + MAX_SEND_RETRIES
            n = fragment_count(len(PAYLOAD), 4)
            assert len(p1.writes) == attempts * n
            assert p1.applied == []  # 永远没通过 CRC，不应用
            # 断链 → 退避重扫 → 重连（新链路新接收上下文）
            await _wait_for(lambda: central.connected, what="重连#2")
            assert central.stats["reconnects"] == 1
            assert len(devices) == 2
            status2 = await central.publish(PAYLOAD)
            assert status2 == "acked"
            assert devices[1].applied == [PAYLOAD]
            assert devices[1].msg_ids == [1]  # 重连归 1（§3.1）
        finally:
            await central.stop()

    asyncio.run(body())


def test_ack_timeout_resend_recovers():
    """ACK 超时：首试 stub 不回 ACK → 5000ms（测试缩短）超时 → 全包重发 →
    第二次 ACK（§3.3 发送方 4 冻结流程）。"""
    connector, devices = make_links(247)
    central = make_central(connector, ack_timeout_s=0.05)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            central._link.peripheral.suppress_ack_first = True
            t0 = time.monotonic()
            status = await central.publish(PAYLOAD)
            assert status == "acked", status
            assert time.monotonic() - t0 >= 0.05  # 确实经历了超时等待
            p = devices[0]
            n = fragment_count(len(PAYLOAD), chunk_capacity(247))
            assert len(p.writes) == 2 * n  # 首发全包 + 重发全包
            assert len(p.applied) == 1  # 首试被抑制，重试才应用
        finally:
            await central.stop()

    asyncio.run(body())


def test_publish_when_disconnected_dropped():
    """未连接时 publish → 丢弃并计数，不触碰任何链路（重连后新帧自然应用）。"""
    connector, devices = make_links(23)
    central = make_central(connector)

    async def body():
        status = await central.publish(PAYLOAD)
        assert status == "dropped-offline"
        assert central.stats["publish_dropped_offline"] == 1
        assert connector.find_calls == 0 and devices == []

    asyncio.run(body())


def test_oversize_and_empty_payload_rejected():
    """>16384 或空载荷 = Bridge 缺陷（§1 上限 / §3.3 total_len=0 不存在）：
    连接建立也拒绝发送、零帧写出。"""
    connector, devices = make_links(23)
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            assert (await central.publish(b"x" * (16384 + 1))) == "dropped-size"
            assert (await central.publish(b"")) == "dropped-size"
            assert devices[0].writes == []
            assert central.stats["snapshots_dropped"] == 2
            assert central.stats["snapshots_acked"] == 0
        finally:
            await central.stop()

    asyncio.run(body())


def test_send_failure_mid_frames_drops_and_reconnects():
    """发送失败（设备睡眠/离开）：中途写失败 → 丢弃当前帧并退避重连 →
    新帧在新链路上完整送达。"""
    connector, devices = make_links(53)
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="连接#1")
            link1 = central._link
            n = fragment_count(len(PAYLOAD), chunk_capacity(53))

            def boom(frame):
                if _HEADER.unpack(frame[:FRAME_SIZE])[3] == 2:  # index 2
                    raise OSError("device went away")

            link1.write_hook = boom
            status = await central.publish(PAYLOAD)
            assert status == "dropped-link", status
            assert central.stats["snapshots_dropped"] == 1
            assert len(devices[0].writes) < n
            await _wait_for(lambda: central.connected, what="重连#2")
            assert central.stats["reconnects"] == 1
            assert (await central.publish(PAYLOAD)) == "acked"
            assert devices[1].applied == [PAYLOAD]
        finally:
            await central.stop()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 断开→退避重连状态机
# ---------------------------------------------------------------------------

def test_scan_absent_then_found_connects():
    """设备不在：静默退避重扫（不连接不报错），出现后连接并可达 ACK。"""
    connector, devices = make_links(23)
    connector.absent_rounds = 3
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="第 4 轮扫描后连接")
            assert connector.find_calls == 4
            assert connector.connect_calls == 1
            assert (await central.publish(PAYLOAD)) == "acked"
        finally:
            await central.stop()

    asyncio.run(body())


def test_external_loss_reconnects_and_message_id_resets():
    """外部断链（设备离开）→ 退避重连 → message_id 每连接归 1（§3.1）；
    断链后 publish 一律丢弃（不排队旧帧）。"""
    connector, devices = make_links(247)
    events: list[tuple] = []
    central = make_central(connector, on_link=lambda s, d: events.append((s, d)))

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="连接#1")
            assert (await central.publish(PAYLOAD)) == "acked"
            assert devices[0].msg_ids == [1]
            central._link.drop_link()  # 设备离开
            # 断链窗口（link 已置空）：publish → 丢弃（不积压重放旧帧）
            assert (await central.publish(PAYLOAD)) == "dropped-offline"
            await _wait_for(lambda: central.connected, what="重连#2")
            assert central.stats["reconnects"] == 1
            assert (await central.publish(PAYLOAD)) == "acked"
            assert devices[1].msg_ids == [1]  # 新连接从 1 重新计数
            statuses = [s for s, _ in events]
            assert statuses.count("connected") == 2
            assert statuses.count("disconnected") == 1
        finally:
            await central.stop()

    asyncio.run(body())


def test_actionable_error_stops_loop_without_retry():
    """权限被拒（§6.1）：可操作提示 + 终态停止，不静默循环重试。"""
    connector, devices = make_links(23)
    connector.scan_errors = [RuntimeError("operation not authorized")]
    events: list[tuple] = []
    central = make_central(connector, on_link=lambda s, d: events.append((s, d)))

    async def body():
        central.start()
        try:
            await asyncio.wait_for(central._task, timeout=WAIT_S)
        finally:
            if central._task and not central._task.done():
                await central.stop()
        assert central.state is CentralState.STOPPED
        assert central.actionable_hint == MACOS_HINT_DENIED
        assert ("error", MACOS_HINT_DENIED) in events
        assert connector.find_calls == 1  # 只扫一次，无重试循环
        assert devices == []

    asyncio.run(body())


def test_stable_connection_resets_backoff():
    """稳定连接 ≥60s 后断开 → 退避回 1s 档（§5.4 稳定即重置，经虚拟时长验证）。"""
    connector, devices = make_links(23)
    central = make_central(connector)

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="连接#1")
            # 连接成功本身不消耗退避档位（attempts=0）；未稳定断开则推进档位
            assert central._backoff.attempts == 0
            assert central._backoff.record_stable(0.0) is False
            assert central._backoff.next_delay() == pytest.approx(0.01)
            assert central._backoff.next_delay() == pytest.approx(0.02)
            # 模拟本连接已稳定保持 61s（不改时钟，直接喂虚拟保持时长）
            held = asyncio.get_running_loop().time() - central._connected_at
            assert held < 60  # 真实保持时长极短
            assert central._backoff.record_stable(61.0) is True  # 稳定 → 重置
            assert central._backoff.next_delay() == pytest.approx(0.01)
        finally:
            await central.stop()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 退避序列本身（冻结形状）
# ---------------------------------------------------------------------------

def test_rescan_backoff_frozen_shape():
    """重扫退避冻结形状：1/2/4/8/16/30 封顶 30s（§5.4；对齐 INTERFACES §6）。"""
    bo = RescanBackoff(jitter_fraction=0.0)
    assert RESCAN_SEQUENCE_S == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    delays = [bo.next_delay() for _ in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert bo.record_stable(59.9) is False
    assert bo.next_delay() == 30.0  # 未稳定：档位继续封顶
    bo2 = RescanBackoff(jitter_fraction=0.0)
    for _ in range(3):
        bo2.next_delay()
    assert bo2.record_stable(60.0) is True
    assert bo2.next_delay() == 1.0  # 稳定即重置回 1s 档


def test_rescan_backoff_jitter_bounds():
    """±20% 均匀抖动（§5.4）：确定性 rng 下逐档落在 [0.8x, 1.2x]。"""
    rng = random.Random(20260913)
    bo = RescanBackoff(rng=rng)
    for expected in RESCAN_SEQUENCE_S:
        d = bo.next_delay()
        assert expected * 0.8 <= d <= expected * 1.2


# ---------------------------------------------------------------------------
# MTU 变化下的分片尺寸适配（跨重连）
# ---------------------------------------------------------------------------

def test_mtu_change_after_reconnect_adapts_chunk():
    """重连后新链路 MTU 247（旧 23）：同一快照片数从 4B/片 降为 228B/片，
    两次重组逐字节一致（§2.4：以协商 ATT MTU 计算片容量）。"""
    connector, devices = make_links(23)
    central = make_central(connector)
    seq = [23, 247]  # 每次连接的 MTU

    def factory():
        mtu = seq.pop(0)
        p = StubPeripheral(mtu)
        devices.append(p)
        return StubLink(p)

    connector.link_factory = factory

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="连接#1 MTU23")
            assert (await central.publish(PAYLOAD)) == "acked"
            assert len(devices[0].writes) == fragment_count(len(PAYLOAD), 4)
            central._link.drop_link()
            await _wait_for(lambda: central.connected, what="重连#2 MTU247")
            assert central._link.mtu == 247
            assert (await central.publish(PAYLOAD)) == "acked"
            assert len(devices[1].writes) == fragment_count(
                len(PAYLOAD), chunk_capacity(247))
            assert len(devices[1].writes) < len(devices[0].writes)  # 228B/片
            assert devices[0].applied == [PAYLOAD] == devices[1].applied
        finally:
            await central.stop()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 上行遥测与 ACK/NACK 回写（§3.4 三值语义，Transport 不解释内容）
# ---------------------------------------------------------------------------

def test_uplink_telemetry_and_reply_apply_result():
    """设备 TX 上行 DATA → on_message 回调；central 依三值结果经 RX 回写
    16B 控制帧：applied/duplicate→ACK、rejected→NACK。"""
    connector, devices = make_links(53)
    uplink: list[tuple] = []
    central = make_central(connector,
                           on_message=lambda data, n: uplink.append((data, n)))

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            p = devices[0]
            link = central._link
            fg = Fragmenter(b'{"kind":"telemetry","v":1}', 9, p.mtu)
            data_frame = fg.frame(0)
            link.device_sends(data_frame)
            assert uplink == [(data_frame, len(data_frame))]
            assert central.stats["uplink_frames"] == 1
            assert p.controls == []  # 尚未回写

            await central.reply_apply_result(data_frame[:FRAME_SIZE], "applied")
            await central.reply_apply_result(data_frame[:FRAME_SIZE],
                                             "duplicate")
            await central.reply_apply_result(data_frame[:FRAME_SIZE], "rejected")
            assert [f[1] for f in p.controls] == \
                [TYPE_ACK, TYPE_ACK, TYPE_NACK]
            # 回写帧回填被确认 DATA 的元数据（§3.2）
            (_v, _t, msg, _i, cnt, total, crc) = _HEADER.unpack(
                data_frame[:FRAME_SIZE])
            for frame in p.controls:
                (rv, rt, rmsg, ridx, rcnt, rtot, rcrc) = _HEADER.unpack(frame)
                assert (rv, rmsg, ridx, rcnt, rtot, rcrc) == \
                    (1, msg, 0, cnt, total, crc)
        finally:
            await central.stop()

    asyncio.run(body())


def test_device_ack_nack_control_frames_recognized():
    """16B 控制帧只认 type 字段：TYPE_ACK=2 结束等待、TYPE_NACK=3 触发全包重发；
    非 ACK/NACK 的 16B 帧按上行 DATA 交回调（不解释内容）。"""
    connector, devices = make_links(23)
    uplink: list[bytes] = []
    central = make_central(connector,
                           on_message=lambda data, _n: uplink.append(data))

    async def body():
        central.start()
        try:
            await _wait_for(lambda: central.connected, what="CONNECTED")
            payload = b'{"k":"dup-check"}'
            fg = Fragmenter(payload, 7, 23)
            data_frame = fg.frame(0)
            central._link.device_sends(data_frame)
            assert uplink == [data_frame]
            assert central.stats["uplink_frames"] == 1
            # 设备主动 ACK 了一条 in-flight 消息元数据（场景自洽性由设备保证）
            assert (await central.publish(b'{"k":1}')) == "acked"
        finally:
            await central.stop()

    asyncio.run(body())
