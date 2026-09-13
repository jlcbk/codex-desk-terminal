"""central.py — BLE central 主机侧（Mac Bleak central）：把 AppState 快照经 BLE 推给设备。

ZC10（A10）。契约真源全部冻结，照抄不发明：protocol/transport.md §2（GATT/UUID/
配对）、§3（16 字节帧头/分片/ACK）、§5.4（退避序列）、§6.1（macOS 权限路径）；
帧编解码复用本包 fragmenter（与 shared/transport/cdt_frame.h 逐规则互为镜像），
连接/配对骨架复用 central_skeleton（B2 真机取证项见其文件头，**真机未验证**）。

信道安全（对应 Wi-Fi 的 TLS+token 条款，transport.md §5.2/§5.5）：本传输不携带
token/证书——契约原句 §2.3「LE Secure Connections（ECDH P-256）+ 数字比较
（Numeric Comparison）+ 绑定」、§2.2「CoreBluetooth 侧无需逐特征配置，由系统
配对机制落实」、INTERFACES §7「加密绑定后才能接受业务数据」。macOS 上 Bleak
连接加密特征（写 RX / 订阅 TX+CCCD）时系统自动走配对；未配对写入被 GATT
权限拒绝（加密+MITM），本模块不绕过、不退化为公开写入。

失败语义（均为契约裁决，不自行发明）：
  - 连接断开/设备不在 → 指数退避重扫，序列 1/2/4/8/16/30s 封顶 30s ±20% 抖动、
    稳定 ≥60s 重置（transport.md §5.4 冻结值；对齐 INTERFACES §6 风格）；
    设备不在只记 DEBUG，不刷屏。
  - 发送失败（设备睡眠/离开）→ 丢弃当前帧并退避重连：设备端 StateStore 有
    seq/epoch 语义（§3.1「重连清空接收上下文」、§1「重连总是取得全量快照」），
    重连后新帧自然应用，旧半包不复活。
  - ACK 超时 5000ms、全包重发最多 2 次，耗尽断开重连/重新同步最新快照
    （§3.3 发送方 4 冻结值）。
  - 权限被拒/蓝牙关闭/配对被拒 → 可操作提示并停止循环，不静默高频重试
    （§6.1 冻结行为）。

红线：Transport 只搬字节——不解释业务 JSON（ACK/NACK 只认 16B 控制帧的
type 字段，§3.4 三值语义由上层 store 回调给出）；日志只含连接状态与发送计数，
无快照内容；凭证不进入任何帧或日志。纯异步（asyncio）；bleak==3.0.2 运行期
懒加载（未安装时 import 本模块不报错，主机测试全部走注入的 stub 连接器）。
"""

from __future__ import annotations

import asyncio
import logging
import random
from enum import Enum

from .central_skeleton import (
    ACK_TIMEOUT_S,
    BLE_RX_CHAR_UUID,
    BLE_SERVICE_UUID,
    BLE_TX_CHAR_UUID,
    MACOS_HINT_DENIED,
    MACOS_HINT_PAIRING,
    MACOS_HINT_POWERED_OFF,
    MAX_SEND_RETRIES,
    _load_bleak,
    classify_connect_error,
)
from .fragmenter import (
    FRAME_SIZE,
    MAX_MESSAGE_LEN,
    TYPE_ACK,
    TYPE_NACK,
    Fragmenter,
    chunk_capacity,
    encode_ack_nack,
)

LOGGER = logging.getLogger("cdt.ble.central")

#: 重扫退避序列（transport.md §5.4 冻结：1/2/4/8/16/30s 封顶 30s，±20% 抖动；
#: 对齐 INTERFACES §6「退避1/2/4/8/16/30s+抖动」风格）。与 wss/link.py 的
#: BACKOFF_SEQUENCE_S 同源同值；此处本地实现以保持 BLE 包零第三方依赖
#: （wss.link 顶层 import websockets，BLE 模块不应被其传染）。
RESCAN_SEQUENCE_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RESCAN_JITTER_FRACTION = 0.2
RESCAN_STABLE_RESET_S = 60.0

#: message_id 为 4 字节字段（§3.1）；本连接内递增，溢出即回卷（工程兜底，
#: 正常节奏不可达）。
_MESSAGE_ID_MAX = 0xFFFFFFFF


class RescanBackoff:
    """重扫退避状态机（§5.4）：连续失败推进档位，稳定连接后重置。

    形状与 wss/link.BackoffPolicy 一致；测试可注入缩小档位（只改等待时长、
    不改序列形状），jitter_fraction=0 时完全确定。
    """

    def __init__(self, sequence_s: tuple = RESCAN_SEQUENCE_S,
                 jitter_fraction: float = RESCAN_JITTER_FRACTION,
                 stable_reset_s: float = RESCAN_STABLE_RESET_S,
                 rng=None) -> None:
        if not sequence_s or any(v <= 0 for v in sequence_s):
            raise ValueError("rescan sequence must be non-empty and positive")
        if any(sequence_s[i] >= sequence_s[i + 1]
               for i in range(len(sequence_s) - 1)):
            raise ValueError("rescan sequence must be strictly increasing")
        if not 0.0 <= jitter_fraction <= 1.0:
            raise ValueError("jitter_fraction must be within [0, 1]")
        if stable_reset_s <= 0:
            raise ValueError("stable_reset_s must be positive")
        self.sequence_s = tuple(float(v) for v in sequence_s)
        self.jitter_fraction = float(jitter_fraction)
        self.stable_reset_s = float(stable_reset_s)
        self._rng = rng if rng is not None else random.Random()
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def next_delay(self) -> float:
        base = self.sequence_s[min(self._attempts, len(self.sequence_s) - 1)]
        self._attempts += 1
        jitter = 1.0 + self._rng.uniform(-self.jitter_fraction,
                                         self.jitter_fraction)
        return base * jitter

    def record_stable(self, held_s: float) -> bool:
        """报告连接保持时长；≥stable_reset_s 视为稳定并重置回 1s 档。"""
        if held_s >= self.stable_reset_s:
            self._attempts = 0
            return True
        return False


class CentralState(Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTED = "connected"
    STOPPED = "stopped"          # 可操作错误（权限/蓝牙关闭/配对被拒）终态


class LinkGone(Exception):
    """链路已失效（写失败/设备离开）；当前帧由 publish 丢弃（契约见模块头）。"""


# ---------------------------------------------------------------------------
# 连接器抽象：真机走 BleakConnector（bleak 懒加载），测试注入 stub（无 BLE 栈）
# ---------------------------------------------------------------------------

class BleakLink:
    """一条已建立连接的最小视图：MTU + 订阅回调 + 写 RX + 断连。

    主机测试以同形状 stub 替换；字段与方法即连接器协议（connector protocol）：
      link.mtu / await link.start_notify(cb) / await link.write(frame, response)
      / await link.disconnect() / link.set_loss_callback(cb)
    """

    def __init__(self, client, rx_char, tx_char, mtu: int) -> None:
        self._client = client
        self._rx_char = rx_char
        self._tx_char = tx_char
        self._mtu = int(mtu)
        self._loss_cb = None

    @property
    def mtu(self) -> int:
        return self._mtu

    async def start_notify(self, cb) -> None:
        await self._client.start_notify(self._tx_char, cb)

    async def write(self, frame: bytes, response: bool = False) -> None:
        # RX 优先 Write Without Response（§2.4：提高吞吐，流控由一次一条
        # 在途 + ACK 承担）；设备须同时接受带响应写。
        await self._client.write_gatt_char(self._rx_char, frame,
                                           response=response)

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        finally:
            self._emit_loss = lambda: None  # 主动断连不再触发 loss 回调

    def set_loss_callback(self, cb) -> None:
        self._loss_cb = cb

    def _emit_loss(self) -> None:
        if self._loss_cb is not None:
            self._loss_cb()


class BleakConnector:
    """真机连接器：扫描（按广播服务 UUID + 名称/地址过滤）→ 连接 → 发现
    冻结 UUID 特征 → 订阅就绪。B2 真机取证项（service_uuids 过滤行为、
    mtu_size 属性名、配对弹窗文案）按 transport.md §8 以实测为准。
    """

    async def find_device(self, name: str | None, address: str | None,
                          service_uuid: str, timeout_s: float):
        """§2.2：只连广播含本服务 UUID 的设备；名称/地址为附加过滤条件。"""
        _BleakClient, BleakScanner = _load_bleak()
        if address:
            return await BleakScanner.find_device_by_address(
                address, timeout=timeout_s)
        want = service_uuid.lower()

        def _match(device, adv) -> bool:
            uuids = [u.lower() for u in (getattr(adv, "service_uuids", None)
                                          or [])]
            if want in uuids:
                return True
            return bool(name) and device.name == name

        return await BleakScanner.find_device_by_filter(_match,
                                                        timeout=timeout_s)

    async def connect(self, handle) -> BleakLink:
        BleakClient, _BleakScanner = _load_bleak()
        client = BleakClient(handle)
        await client.connect()
        try:
            # MTU 协商结果（macOS 通常 ≥185；B5 记录正常/最坏两档实测值）
            mtu = int(getattr(client, "mtu_size", 23) or 23)
            # §2.2/§2.3：macOS 无显式 pair API——写加密特征时系统自动走
            # 配对（LE Secure Connections 数字比较 + 绑定）；Windows 后端
            # 的 pair() 可用则显式触发。不捕获配对拒绝：交上层按 §6.1
            # 给可操作提示，不退化为未加密连接。
            pair = getattr(client, "pair", None)
            if pair is not None:
                try:
                    await pair()
                except NotImplementedError:
                    pass
            service_id = BLE_SERVICE_UUID.replace("-", "")
            rx = tx = None
            for service in client.services:
                if service.uuid.upper().replace("-", "") != service_id:
                    continue
                for char in service.characteristics:
                    u = char.uuid.upper()
                    if u == BLE_RX_CHAR_UUID:
                        rx = char
                    elif u == BLE_TX_CHAR_UUID:
                        tx = char
            if rx is None or tx is None:
                raise RuntimeError(
                    "冻结 UUID 的 RX/TX 特征未找到（固件与协议不一致，§2.1）")
            link = BleakLink(client, rx, tx, mtu)
            if hasattr(client, "set_disconnected_callback"):
                client.set_disconnected_callback(lambda _c: link._emit_loss())
            # TX 订阅由 BleakCentral._enter_link 统一执行（link.start_notify）。
            return link
        except Exception:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — 清理路径
                pass
            raise


# ---------------------------------------------------------------------------
# BleakCentral
# ---------------------------------------------------------------------------

class BleakCentral:
    """BLE 主机侧中央：扫描→连接→MTU 协商→系统配对→订阅 TX→分片写 RX→ACK。

    与 wss server 的 publisher 风格一致：单消费者对 publish(data) 逐份调用，
    一次仅 1 条在途消息（§3.3 发送方 4，另加发送锁兜底）。run()/start() 常驻
    处理连接生命周期与退避重扫；publish() 只在已连接时发送，其余情况丢弃
    （重连后设备按新帧自然应用——§1 重连总是取得全量快照）。

    日志只含连接状态与发送计数（无快照内容，无凭证）。
    """

    def __init__(self, device_name: str = "CodexDT",
                 device_address: str | None = None, *,
                 connector=None,
                 backoff: RescanBackoff | None = None,
                 scan_timeout_s: float = 10.0,
                 ack_timeout_s: float = ACK_TIMEOUT_S,
                 max_send_retries: int = MAX_SEND_RETRIES,
                 on_link=None, on_message=None, rng=None) -> None:
        self.device_name = device_name
        self.device_address = device_address
        self._connector = connector if connector is not None else BleakConnector()
        self._backoff = backoff or RescanBackoff(rng=rng)
        self.scan_timeout_s = scan_timeout_s
        self.ack_timeout_s = ack_timeout_s
        self.max_send_retries = max_send_retries
        # on_link(status, detail)：status ∈ connected/disconnected/error
        # （error=§6.1 可操作提示，随后循环终态停止，不静默重试）。
        self.on_link = on_link
        # on_message(frame_bytes, n)：设备上行遥测 DATA 交上层（§3.4 三值
        # 回调后经 reply_apply_result 回 ACK/NACK）；本模块不解释内容。
        self.on_message = on_message

        self.state = CentralState.DISCONNECTED
        self.stats = {
            "scans": 0,
            "connects": 0,
            "reconnects": 0,
            "snapshots_acked": 0,
            "snapshots_dropped": 0,       # 发送失败丢弃（设备睡眠/离开/重试耗尽）
            "publish_dropped_offline": 0, # 未连接时 publish 丢弃
            "nack_retries": 0,
            "uplink_frames": 0,
        }

        self._link = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._link_lost = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._ack_frame: bytes | None = None
        self._ack_event = asyncio.Event()
        self._next_message_id = 1  # 本次连接内递增；重连后归 1（§3.1）
        self._connected_at = 0.0
        self._actionable_hint: str | None = None

    # -- 生命周期 -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._link is not None and self.state is CentralState.CONNECTED

    @property
    def actionable_hint(self) -> str | None:
        return self._actionable_hint

    def start(self) -> None:
        """启动常驻连接循环（幂等）。"""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(
                self.run(), name="cdt-ble-central")

    async def stop(self) -> None:
        """停机：退出重扫循环并断链（幂等）。"""
        self._stopping = True
        link, self._link = self._link, None
        self._set_state(CentralState.STOPPED)
        self._link_lost.set()
        self._ack_event.set()  # 唤醒可能在等的发送方
        if link is not None:
            try:
                await link.disconnect()
            except Exception:  # noqa: BLE001 — 停机清理路径
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        """常驻连接循环：扫描→连接→订阅→等断链→退避重扫（§5.4 序列）。"""
        while not self._stopping:
            self._set_state(CentralState.SCANNING)
            self.stats["scans"] += 1
            try:
                handle = await self._connector.find_device(
                    self.device_name, self.device_address, BLE_SERVICE_UUID,
                    self.scan_timeout_s)
            except Exception as exc:  # noqa: BLE001 — §6.1 错误分类
                if self._handle_actionable(exc):
                    return
                LOGGER.debug("扫描失败（退避重试）：%s", exc)
                await self._sleep_backoff()
                continue
            if handle is None:
                # 设备不在：静默退避不刷屏（DEBUG 一行/轮）
                LOGGER.debug("未发现设备 name=%s service=%s（退避 %.1fs 后重扫）",
                             self.device_name, BLE_SERVICE_UUID,
                             self._backoff.sequence_s[0])
                await self._sleep_backoff()
                continue

            self.stats["connects"] += 1
            try:
                link = await self._connector.connect(handle)
            except Exception as exc:  # noqa: BLE001 — §6.1 错误分类
                if self._handle_actionable(exc):
                    return
                LOGGER.warning("连接失败（退避重试）：%s", exc)
                await self._sleep_backoff()
                continue

            try:
                await self._enter_link(link)
            except Exception as exc:  # noqa: BLE001 — MTU 过小等连接级失败
                LOGGER.warning("连接初始化失败（退避重试）：%s", exc)
                await self._sleep_backoff()
                continue
            await self._link_lost.wait()
            self._link_lost.clear()
            link, self._link = self._link, None
            held_s = asyncio.get_running_loop().time() - self._connected_at
            self._backoff.record_stable(held_s)
            self._set_state(CentralState.DISCONNECTED)
            self.stats["reconnects"] += 1
            LOGGER.info("BLE 断开 held=%.1fs 重连序号=%d（退避重扫）",
                        held_s, self.stats["reconnects"])
            self._fire_link("disconnected", "")
            if self._stopping:
                break
            if link is not None:
                try:
                    await link.disconnect()
                except Exception:  # noqa: BLE001 — 清理路径
                    pass
            await self._sleep_backoff()

    async def _sleep_backoff(self) -> None:
        delay = self._backoff.next_delay()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if not self._stopping:
                raise

    def _handle_actionable(self, exc: Exception) -> bool:
        """权限被拒/蓝牙关闭/配对被拒：可操作提示 + 终态停止（§6.1 不静默重试）。"""
        hint = classify_connect_error(exc)
        if hint not in (MACOS_HINT_DENIED, MACOS_HINT_POWERED_OFF,
                        MACOS_HINT_PAIRING):
            return False
        self._actionable_hint = hint
        LOGGER.error("BLE 不可用：%s", hint)
        self._set_state(CentralState.STOPPED)
        self._fire_link("error", hint)
        return True

    async def _enter_link(self, link) -> None:
        chunk = chunk_capacity(link.mtu)
        if chunk <= 0:
            try:
                await link.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("协商 MTU %d 过小：片容量必须 >0（§2.4）" % link.mtu)
        self._link = link
        self._next_message_id = 1      # 重连清 message_id 上下文（§3.1/§3.3 接收方 6）
        self._ack_frame = None
        self._ack_event = asyncio.Event()
        self._link_lost = asyncio.Event()
        link.set_loss_callback(self._on_external_loss)
        await link.start_notify(self._on_notify)
        self._connected_at = asyncio.get_running_loop().time()
        self._set_state(CentralState.CONNECTED)
        LOGGER.info("BLE 已连接 name=%s mtu=%d chunk=%dB（系统配对加密信道，§2.3）",
                    self.device_name, link.mtu, chunk)
        self._fire_link("connected", "mtu=%d" % link.mtu)

    def _on_external_loss(self) -> None:
        """连接器异步断链回调（设备侧断开/超距）：唤起 run 循环清理。"""
        link = self._link
        if link is None:
            return
        self._link = None
        self._link_lost.set()
        self._ack_event.set()

    def _set_state(self, state: CentralState) -> None:
        self.state = state

    def _fire_link(self, status: str, detail: str) -> None:
        if self.on_link is not None:
            try:
                self.on_link(status, detail)
            except Exception:  # noqa: BLE001 — 回调异常不拖垮循环
                LOGGER.exception("on_link 回调异常")

    # -- TX（设备→central）接收 ---------------------------------------------

    def _on_notify(self, _sender, data: bytearray) -> None:
        """TX 通知：16B 控制帧=ACK/NACK（只认 type 字节）；其余为上行 DATA。"""
        frame = bytes(data)
        if len(frame) == FRAME_SIZE and frame[1] in (TYPE_ACK, TYPE_NACK):
            self._ack_frame = frame
            self._ack_event.set()
            return
        self.stats["uplink_frames"] += 1
        if self.on_message is not None:
            self.on_message(frame, len(frame))

    async def reply_apply_result(self, confirmed_header: bytes,
                                 result: str) -> None:
        """对收到的设备 DATA（遥测）按上层三值结果回 ACK/NACK（§3.4，经 RX 写回）。

        result ∈ applied/duplicate/rejected；Transport 不解析业务内容。
        """
        link = self._link
        if link is None:
            raise RuntimeError("not connected")
        await link.write(encode_ack_nack(confirmed_header, result),
                         response=True)

    # -- RX（central→设备）发送 ----------------------------------------------

    async def publish(self, payload: bytes) -> str:
        """发送一份完整 AppState 快照（全量，§1）。

        分片（fragmenter，CRC 覆盖完整 payload）→ 逐片 Write Without
        Response → 等 ACK（5000ms 超时，全包重发 ≤2 次，§3.3 发送方 4）。

        返回状态字符串：
          "acked"          设备确认（applied/duplicate 都回 ACK，§3.4）
          "dropped-offline" 未连接：丢弃（重连后新帧自然应用）
          "dropped-link"   写失败（设备睡眠/离开）：丢弃当前帧并触发重连
          "dropped-nack"   重试耗尽：丢弃当前帧并断链重同步最新快照
          "dropped-size"   空载荷或超 16384 字节（Bridge 缺陷，§1 上限）
        """
        if not self.connected:
            self.stats["publish_dropped_offline"] += 1
            LOGGER.debug("未连接，快照丢弃 offline_dropped=%d（重连后新帧自然应用）",
                         self.stats["publish_dropped_offline"])
            return "dropped-offline"
        if not payload or len(payload) > MAX_MESSAGE_LEN:
            # Bridge 缺陷（§1 聚合上限；§3.3 total_len=0 不存在）：不发送
            LOGGER.error("快照尺寸非法 bytes=%d（上限 %d），拒绝发送",
                         len(payload), MAX_MESSAGE_LEN)
            self.stats["snapshots_dropped"] += 1
            return "dropped-size"

        async with self._send_lock:  # 一次仅 1 条在途消息（§3.3 发送方 4）
            try:
                ok = await self._send_with_retries(payload)
            except LinkGone as exc:
                self.stats["snapshots_dropped"] += 1
                LOGGER.warning("发送失败（%s）：丢弃当前帧并重连；"
                               "设备重连后按新帧自然应用（seq/epoch 语义）",
                               exc)
                self._teardown_link()
                return "dropped-link"
            if ok:
                self.stats["snapshots_acked"] += 1
                return "acked"
            # 重试耗尽（§3.3 发送方 4：断开重连/重新同步最新快照）
            self.stats["snapshots_dropped"] += 1
            LOGGER.warning("ACK 重试耗尽：丢弃当前帧并断链重同步"
                           "（设备重连后按新帧自然应用）")
            self._teardown_link()
            return "dropped-nack"

    def _teardown_link(self) -> None:
        """丢弃当前帧的善后：断链并唤起 run 循环进入退避重扫。"""
        link, self._link = self._link, None
        self._link_lost.set()
        self._ack_event.set()
        if link is not None:
            async def _disconnect():
                try:
                    await link.disconnect()
                except Exception:  # noqa: BLE001 — 清理路径
                    pass
            asyncio.get_running_loop().create_task(_disconnect())

    async def _send_with_retries(self, payload: bytes) -> bool:
        link = self._link
        if link is None:
            raise LinkGone("link vanished before send")
        message_id = self._next_message_id
        self._next_message_id = \
            message_id + 1 if message_id < _MESSAGE_ID_MAX else 1
        fg = Fragmenter(payload, message_id, link.mtu)

        for attempt in range(self.max_send_retries + 1):  # 初发 + 2 次全包重发
            self._ack_frame = None
            self._ack_event = asyncio.Event()
            for index in range(fg.count):
                frame = fg.frame(index)
                try:
                    await link.write(frame, response=False)
                except Exception as exc:  # noqa: BLE001 — 写失败即链路失效
                    raise LinkGone("write fragment %d/%d failed: %s"
                                   % (index + 1, fg.count, exc)) from exc
            LOGGER.debug("已发出 message_id=%d frames=%d bytes=%d 尝试=%d",
                         message_id, fg.count, fg.total_len, attempt + 1)
            try:
                ack = await asyncio.wait_for(self._wait_ack(),
                                             timeout=self.ack_timeout_s)
            except asyncio.TimeoutError:
                LOGGER.info("ACK 超时（%.1fs）message_id=%d：全包重发 %d/%d",
                            self.ack_timeout_s, message_id, attempt + 1,
                            self.max_send_retries)
                continue  # §3.3 发送方 4：全包重发
            if ack[1] == TYPE_ACK:
                LOGGER.info("快照已确认 message_id=%d frames=%d bytes=%d "
                            "acked_total=%d", message_id, fg.count,
                            fg.total_len, self.stats["snapshots_acked"] + 1)
                return True
            self.stats["nack_retries"] += 1
            LOGGER.info("收到 NACK message_id=%d：全包重发 %d/%d",
                        message_id, attempt + 1, self.max_send_retries)
            continue  # NACK → 全包重发（§3.2/§3.3）
        return False

    async def _wait_ack(self) -> bytes:
        await self._ack_event.wait()
        ack, self._ack_frame = self._ack_frame, None
        if ack is None:
            raise LinkGone("woken without ack (link teardown)")
        return ack
