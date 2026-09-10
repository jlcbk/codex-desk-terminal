"""central_skeleton.py — BLE central（macOS Bridge 侧）骨架：P3.4 步骤 B2 前置。

**状态：真机未验证。** 本文件只落实 protocol/transport.md §2（GATT/UUID/配对）
与 §6.1（macOS 权限路径）的骨架与错误分类，供 B2 真机小样填充实测：
bleak 3.x 相对 1.x 的 API 差异以 B2 实测为准（transport.md §8），届时按实测修订。

不连真机、不做扫描重试循环；生产接入前的所有 IO 都集中在 async 方法里，
便于 B2 用真实 peripheral 逐项取证（配对弹窗、MTU 实测值、吞吐）。

UUID 为 P0.5 冻结值（transport.md §2.1，UUIDv5 可复现，永不再变）：
  Service B931F216-B7FD-50E9-8C33-F1416ADE3B1D
  RX      890AAC2C-3C19-5200-BDEC-74943BA536A1   （central → 设备，Write）
  TX      8EABE170-A4E7-5C26-A287-6F5871014707   （设备 → central，Notify）

红线：本模块只搬字节；不解释业务 JSON；凭证不进入日志。
纯标准库 + 运行期可选 bleak==3.0.2（未安装时仅 import 本模块不报错，
调用连接方法才要求安装——host 测试环境无 BLE 栈）。
"""

from __future__ import annotations

import asyncio
import sys

from .fragmenter import (FRAME_SIZE, Fragmenter, chunk_capacity, encode_ack_nack)

# P0.5 冻结 UUID（transport.md §2.1）——不得改动、不得派生新值。
BLE_SERVICE_UUID = "B931F216-B7FD-50E9-8C33-F1416ADE3B1D"
BLE_RX_CHAR_UUID = "890AAC2C-3C19-5200-BDEC-74943BA536A1"
BLE_TX_CHAR_UUID = "8EABE170-A4E7-5C26-A287-6F5871014707"

# 冻结参数（transport.md §3.3 发送方 4）
ACK_TIMEOUT_S = 5.0
MAX_SEND_RETRIES = 2

MACOS_HINT_DENIED = ("系统设置 → 隐私与安全性 → 蓝牙 → 为终端 App 开启，"
                     "然后重启 Bridge 进程")
MACOS_HINT_POWERED_OFF = "系统设置 → 蓝牙 打开"
MACOS_HINT_PAIRING = ("设备端配对被拒或未完成：在设备上长按 KEY 确认数字比较，"
                      "或删除既有绑定后重新配对（不退化为未加密连接）")


def _load_bleak():
    """运行期懒加载 bleak（3.0.2；版本锁见 docs/VERSIONS.md）。

    B2 未实测项：3.x 的 BleakScanner service_uuids 过滤与 BleakClient.mtu_size
    属性名以实测为准；如 API 有破坏性差异，按 transport.md §8 记录并重评。
    """
    try:
        from bleak import BleakClient, BleakScanner  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "bleak 未安装：uv run --with bleak==3.0.2 python -m bridge ... "
            "（Bridge 运行时 CPython 3.12）") from exc
    from bleak import BleakClient, BleakScanner
    return BleakClient, BleakScanner


def check_macos_bluetooth_permission() -> str:
    """macOS 蓝牙授权预检（transport.md §6.1）。返回可操作提示或 "ok"。

    真机未验证：CoreBluetooth 绑定经由 bleak 自带的 pyobjc；授权归属承载
    进程（Terminal/iTerm/IDE），首次使用时系统弹窗，允许一次长期有效。
    打包为 .app 时 Info.plist 必须含 NSBluetoothAlwaysUsageDescription。
    数值→含义映射按 CBManagerAuthorization 枚举书写，B2 实测确认。
    """
    if sys.platform != "darwin":
        return "ok"
    try:
        from CoreBluetooth import CBManager  # type: ignore
    except ImportError:
        return "pyobjc 不可用（bleak 未安装？）；无法预检授权状态"
    auth = int(CBManager.authorization())
    # 0=notDetermined 1=denied 2=restricted 3=allowedAlways（B2 实测核对）
    mapping = {
        0: "首次使用将弹授权框",
        1: MACOS_HINT_DENIED,
        2: MACOS_HINT_DENIED,
        3: "ok",
    }
    return mapping.get(auth, "授权状态未知（%r）；如扫描失败按：%s"
                       % (auth, MACOS_HINT_DENIED))


def classify_connect_error(exc: Exception) -> str:
    """把 bleak/CoreBluetooth 异常翻译成 §6.1 的可操作提示（真机未验证）。"""
    text = "%s %s" % (type(exc).__name__, exc)
    low = text.lower()
    if "unauthorized" in low or "not authorized" in low or "permission" in low:
        return MACOS_HINT_DENIED
    if "poweredoff" in low.replace("_", "") or "powered off" in low:
        return MACOS_HINT_POWERED_OFF
    if "pairing" in low or "authentication" in low or "insufficient" in low:
        return MACOS_HINT_PAIRING
    return "连接失败：%s" % text


class BleCentralSkeleton:
    """bleak central 骨架：连接 → 订阅 TX → MTU 协商 → 写 RX（分片+ACK）。

    真机未验证项（B2 取证清单，对应 transport.md §7.2）：
      B0 macOS 蓝牙授权弹窗与文案；B2 LE Secure Connections 数字比较 +
         KEY 确认可行性；B3 16KiB 大包在协商 MTU 下的 ACK 收敛；
         B5 MTU23/正常 MTU 两种吞吐指标。
    """

    def __init__(self, device_name: str = "CodexDT"):
        self.device_name = device_name
        self.client = None
        self.mtu = 23            # 协商前按最小 MTU 假设（协议正确性不靠大 MTU）
        self.rx_char = None
        self.tx_char = None
        self._tx_queue = asyncio.Queue()
        self._next_message_id = 1  # 本次连接内递增；重连后归 1（重连清上下文）
        self._pending_ack = None

    # -- 连接生命周期 -----------------------------------------------------

    async def connect(self, scan_timeout: float = 10.0) -> None:
        """扫描（按服务 UUID 过滤）→ 连接 → 发现服务 → 订阅 TX。

        真机未验证：只连广播含冻结服务 UUID 且已绑定过的设备（§2.2）；
        未配对连接写入 RX 应被 GATT 权限拒绝（加密+MITM），本骨架不绕过。
        """
        BleakClient, BleakScanner = _load_bleak()
        try:
            device = await BleakScanner.find_device_by_name(
                self.device_name, timeout=scan_timeout)
            # B2 取证项：改为按服务 UUID 过滤（BleakScanner(service_uuids=[…])
            # 或 find_device_by_filter），落实 §2.2"只连广播含本服务 UUID 且
            # 已绑定过的设备"；本骨架先按设备名发现，方便首连调试。
        except Exception as exc:  # noqa: BLE001 — 骨架期统一分类
            raise RuntimeError(classify_connect_error(exc)) from exc
        if device is None:
            raise RuntimeError("未发现名为 %s 的设备（广播应含服务 %s）"
                               % (self.device_name, BLE_SERVICE_UUID))

        self.client = BleakClient(device)
        await self.client.connect()
        # MTU 协商结果（macOS 通常 ≥185；B5 记录实测值，正常/最坏两档）
        self.mtu = int(getattr(self.client, "mtu_size", 23))
        if chunk_capacity(self.mtu) <= 0:
            await self.disconnect()
            raise RuntimeError("协商 MTU %d 过小：片容量必须 >0" % self.mtu)

        services = self.client.services
        for service in services:
            if service.uuid.upper().replace("-", "") == \
                    BLE_SERVICE_UUID.replace("-", ""):
                for char in service.characteristics:
                    u = char.uuid.upper()
                    if u == BLE_RX_CHAR_UUID:
                        self.rx_char = char
                    elif u == BLE_TX_CHAR_UUID:
                        self.tx_char = char
        if self.rx_char is None or self.tx_char is None:
            await self.disconnect()
            raise RuntimeError("冻结 UUID 的 RX/TX 特征未找到（固件与协议不一致）")

        await self.client.start_notify(self.tx_char, self._on_tx_notify)

    async def disconnect(self) -> None:
        """断连：立刻清空在途半包与 message_id 计数（§3.3 接收方 6、§1 重连语义）。"""
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None
        self._next_message_id = 1
        self._pending_ack = None

    # -- TX（设备→central）接收 -------------------------------------------

    def _on_tx_notify(self, _sender, data: bytearray) -> None:
        """TX 通知回调：只区分 16B 控制帧与 DATA 帧，不解释业务内容。

        ACK/NACK 驱动发送方重试状态机（§3.4：ACK≠批准 Codex 操作，
        仅表示本地静音/已读层面的应用确认）。
        """
        if len(data) == FRAME_SIZE:
            self._pending_ack = bytes(data)
            return
        # 设备上行遥测 DATA：交上层 store 三值回调后经 RX 回 ACK/NACK（B3 取证）
        self._tx_queue.put_nowait(bytes(data))

    # -- RX（central→设备）发送 -------------------------------------------

    async def reply_apply_result(self, confirmed_header: bytes, result: str) -> None:
        """对收到的设备 DATA（遥测）回 ACK/NACK（§3.4：经 RX 写回 16B 帧）。

        result 为上层 store 的三值信号 applied/duplicate/rejected；
        Transport 不解析业务内容。真机未验证。
        """
        if self.client is None or self.rx_char is None:
            raise RuntimeError("not connected")
        await self.client.write_gatt_char(self.rx_char,
                                          encode_ack_nack(confirmed_header, result),
                                          response=True)

    async def send_message(self, payload: bytes) -> str:
        """发送一条完整消息：分片 → 逐片写 RX → 等 ACK → 最多重发 2 次。

        - 一次仅 1 条在途消息（§3.3 发送方 4）；RX 优先 Write Without
          Response（response=False），设备须同时接受带响应写。
        - ACK 超时 5000ms；重试为全包重发；耗尽则断开重连/重新同步。
        - 设备侧对收到包的三值判定（applied/duplicate/rejected，§3.4）
          经 TX 通知返回 ACK/NACK；本骨架只认帧类型，不读业务内容。
        返回 "acked"/"nacked"/"timeout"。真机未验证。
        """
        if self.client is None or self.rx_char is None:
            raise RuntimeError("not connected")
        message_id = self._next_message_id
        self._next_message_id += 1
        fg = Fragmenter(payload, message_id, self.mtu)
        first_header = fg.header(0)

        for attempt in range(MAX_SEND_RETRIES + 1):  # 初发 + 2 次重发全包
            self._pending_ack = None
            for index in range(fg.count):
                frame = fg.frame(index)
                await self.client.write_gatt_char(self.rx_char, frame,
                                                  response=False)
            try:
                ack = await asyncio.wait_for(self._wait_ack(first_header),
                                             timeout=ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue  # 5000ms ACK 超时 → 全包重发
            if ack is None:
                return "timeout"
            ack_type = ack[1]
            if ack_type == 2:  # ACK
                return "acked"
            # NACK → 全包重发（§3.3 发送方 4）
        return "nacked"  # 重试耗尽：上层应断开重连/重新同步最新快照

    async def _wait_ack(self, _first_header: bytes):
        """等待 TX 上的 ACK/NACK 控制帧（骨架轮询实现，B2 可改为 Event）。"""
        while True:
            if self._pending_ack is not None:
                ack, self._pending_ack = self._pending_ack, None
                return ack
            await asyncio.sleep(0.01)
