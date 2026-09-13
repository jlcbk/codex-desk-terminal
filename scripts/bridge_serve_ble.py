#!/usr/bin/env python3
"""bridge_serve_ble.py — ZCode 观察源的 **BLE 传输**服务入口（ZC10，A10）。

镜像 bridge_serve_zcode.py 的服务编排（不复制：PublishBridge/make_sink/
initial_snapshot/observer_worker 等公共逻辑直接 import 自该脚本），只把
WSS 广播换成 BLE 推送：

  StateEngine(epoch="zcode-<启动毫秒>", source_kind=SOURCE_ZCODE_OBSERVED,
              utc_anchor_ms=<wall-mono 固定锚点>)
  + ZcodeObserver（run_forever 每份快照回调 sink）
  → ServeHub 盖章 wire epoch/seq → 有界发布桥（单消费者保帧序）
  → BleakCentral（Mac 为 BLE central）分片写 RX 推给设备。

传输与安全（protocol/transport.md 冻结）：
  - 扫描→连接→MTU 协商→订阅 TX→分片写 RX→等 ACK；断开/设备不在按 §5.4
    序列 1/2/4/8/16/30s ±20% 退避重扫；发送失败丢弃当前帧（设备端
    StateStore 有 seq/epoch 语义，重连后新帧自然应用）。
  - 信道安全 = BLE 配对加密（§2.3「LE Secure Connections + 数字比较 +
    绑定」；对应 Wi-Fi 的 TLS+token 条款）——**无 token、无证书**，
    未配对写入被 GATT 权限（加密+MITM）拒绝，本服务不提供绕过项。
  - 一次仅一台设备（GATT 连接语义）；--device-name/--address 只做发现过滤。

契约注意（与 serve_zcode 同口径）：
  - 观察器/bleak 均运行期才真正使用；--dry-run 不构造观察器、不触发扫描。
  - token 不存在（BLE 无 token）；spool 默认与 hook 写入端同源
    （CDT_HOOK_SPOOL 优先，否则 ~/.zcode/cli/cdt-hook-spool.jsonl）。
  - 设备缺席时快照在发布桥丢弃（publish 计数），不积压不重放。

用法：
  # 冒烟（无设备环境：打印名称/UUID 与流程摘要，退出 0）
  python3 scripts/bridge_serve_ble.py --dry-run
  # 真机（P3.4 B2 之后；设备名默认 CodexDT，广播含冻结服务 UUID）
  uv run --python 3.12 --with bleak==3.0.2 python3 scripts/bridge_serve_ble.py \
    --device-name CodexDT --poll-interval 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge.serve_codex import ServeHub  # noqa: E402（服务编排复用：盖章层）
from bridge.state.engine import SOURCE_ZCODE_OBSERVED, StateEngine  # noqa: E402
from bridge.transports.ble.central import (  # noqa: E402
    RESCAN_SEQUENCE_S,
    BLE_RX_CHAR_UUID,
    BLE_SERVICE_UUID,
    BLE_TX_CHAR_UUID,
    BleakCentral,
    RescanBackoff,
)

LOGGER = logging.getLogger("cdt.serve_ble")


def load_zcode_serve():
    """按路径加载 scripts/bridge_serve_zcode.py 复用其服务编排公共逻辑
    （PublishBridge/make_sink/initial_snapshot/observer_worker 等）。
    该脚本顶层不 import websockets 与 ZC1 观察器（其契约），加载安全。"""
    path = REPO_ROOT / "scripts" / "bridge_serve_zcode.py"
    spec = importlib.util.spec_from_file_location("cdt_serve_zcode_shared",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ZC = load_zcode_serve()  # 公共逻辑单一来源：serve_zcode 本体（不许碰、不复制）


# ---------------------------------------------------------------------------
# --dry-run（不起 BLE、不构造观察器：无 bleak 依赖、无系统蓝牙调用）
# ---------------------------------------------------------------------------

def run_dry_run(args: argparse.Namespace) -> int:
    spool = Path(args.spool).expanduser() if args.spool else Path(
        ZC.resolve_spool_path()).expanduser()
    rollout = Path(args.rollout_dir).expanduser() if args.rollout_dir else Path(
        ZC.DEFAULT_ROLLOUT_DIR).expanduser()
    agents = Path(args.agents_dir).expanduser() if args.agents_dir else Path(
        ZC.DEFAULT_AGENTS_DIR).expanduser()

    def _exists(p: Path) -> str:
        return "存在" if p.exists() else "缺失"

    epoch = args.epoch or ("zcode-%d" % int(time.time() * 1000))
    print("bridge_serve_ble --dry-run（不扫描、不连接、不构造观察器）")
    print("source_kind: %s（与 serve_zcode 同源）" % SOURCE_ZCODE_OBSERVED)
    print("epoch: %s" % epoch)
    print("device_filter: name=%s address=%s" % (args.device_name,
                                                 args.address or "未指定"))
    print("service_uuid: %s（P0.5 冻结，§2.1；扫描按广播服务 UUID 过滤）"
          % BLE_SERVICE_UUID)
    print("rx_char_uuid: %s（central→设备，Write；分片帧=16B 头+载荷）"
          % BLE_RX_CHAR_UUID)
    print("tx_char_uuid: %s（设备→central，Notify；ACK/NACK=16B 控制帧）"
          % BLE_TX_CHAR_UUID)
    print("mtu_rule: chunk = ATT_MTU − 3 − 16（§2.4；MTU23 时 4B/片，"
          "协议正确性不靠大 MTU 掩盖）")
    print("security: BLE 配对加密即信道安全（§2.3 LE Secure Connections + "
          "数字比较 + 绑定；对应 Wi-Fi 的 TLS+token 条款——无 token/证书）")
    print("rescan_backoff: %s 秒 ±20%% 抖动，稳定 ≥60s 重置（§5.4）"
          % "/".join(str(int(v)) for v in RESCAN_SEQUENCE_S))
    print("flow: 扫描 → 连接 → MTU 协商 → 系统配对 → 订阅 TX → "
          "分片写 RX → 等 ACK →（断开）退避重扫")
    print("rollout_dir: %s (%s)" % (rollout, _exists(rollout)))
    print("agents_dir: %s (%s)" % (agents, _exists(agents)))
    print("spool: %s (%s; 来源=%s)" % (
        spool, _exists(spool),
        "CDT_HOOK_SPOOL" if (not args.spool and os.environ.get("CDT_HOOK_SPOOL"))
        else ("--spool" if args.spool else "默认")))
    print("poll_interval: %ss" % args.poll_interval)
    print("dry-run PASS（退出 0；真实服务需设备在广播且完成配对绑定）")
    return 0


# ---------------------------------------------------------------------------
# serve：观察器线程 + asyncio 接线（发布桥与 sink 直接复用 serve_zcode）
# ---------------------------------------------------------------------------

def make_publisher_loop(queue: asyncio.Queue, central: BleakCentral):
    """单消费者按序推送（与 serve_zcode 的 _publisher_loop 同风格）；
    publish 的三值结果只记状态与计数，不打印快照内容。"""

    async def _loop() -> None:
        while True:
            data = await queue.get()
            status = await central.publish(data)
            if status == "acked":
                LOGGER.info("BLE 快照已送达 bytes=%d acked=%d", len(data),
                            central.stats["snapshots_acked"])
            elif status == "dropped-size":
                LOGGER.error("BLE 快照尺寸非法被拒（超 16KiB，Bridge 缺陷）")
            else:
                LOGGER.warning("BLE 快照未送达 status=%s dropped=%d "
                               "offline_dropped=%d（设备重连后新帧自然应用）",
                               status, central.stats["snapshots_dropped"],
                               central.stats["publish_dropped_offline"])

    return _loop


def on_link_logger(central: BleakCentral):
    """连接状态日志（只含状态与计数，无快照内容；§6.1 error 路径可操作提示
    已由 central 分类并终态停止，这里只落 ERROR 一行）。"""
    def _on_link(status: str, detail: str) -> None:
        if status == "connected":
            LOGGER.info("BLE link connected %s", detail)
        elif status == "error":
            LOGGER.error("BLE link error（终态，不重试）：%s", detail)
        else:
            LOGGER.info("BLE link %s reconnects=%d", status,
                        central.stats["reconnects"])
    return _on_link


async def amain(args: argparse.Namespace) -> int:
    wall_ms = int(time.time() * 1000)
    mono_ms = int(time.monotonic() * 1000)
    epoch = args.epoch or ("zcode-%d" % wall_ms)
    anchor_ms = wall_ms - mono_ms  # 固定锚点（确定性输出，同 serve_zcode）
    engine = StateEngine(epoch, source_kind=SOURCE_ZCODE_OBSERVED,
                         utc_anchor_ms=anchor_ms)
    hub = ServeHub(base_epoch=epoch)  # 盖章层：同 epoch 字符串，字节中立
    bridge = ZC.PublishBridge()

    central = BleakCentral(
        args.device_name, args.address or None,
        backoff=RescanBackoff(),
        scan_timeout_s=args.scan_timeout,
    )
    # on_link 需引用 central 自身计数，构造后接线：
    central.on_link = on_link_logger(central)

    spool_path = str(Path(args.spool).expanduser()) if args.spool \
        else ZC.resolve_spool_path()

    bridge.start(asyncio.get_running_loop())
    # 初始 IDLE 存活快照（threads 空）：连接建立后的首帧即全量（§1）。
    initial_data = hub.publish(ZC.initial_snapshot(hub.epoch, anchor_ms,
                                                   mono_ms))
    publisher = asyncio.create_task(make_publisher_loop(bridge.queue, central),
                                    name="cdt-serve-ble-publisher")
    bridge.submit(initial_data)

    central.start()

    observer = None
    if not args.no_observer:
        observer = threading.Thread(
            target=ZC.observer_worker, args=(args, engine,
                                             ZC.make_sink(hub, bridge),
                                             spool_path),
            name="cdt-serve-ble-observer", daemon=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # 非 POSIX
            pass

    if observer is not None:
        observer.start()
    print(
        "bridge_serve_ble: BLE central name=%s address=%s service=%s "
        "epoch=%s source=%s spool=%s poll=%ss backoff=%s "
        "(BLE 配对加密=信道安全，§2.3)" % (
            args.device_name, args.address or "未指定", BLE_SERVICE_UUID,
            epoch, SOURCE_ZCODE_OBSERVED, spool_path, args.poll_interval,
            "/".join(str(int(v)) for v in RESCAN_SEQUENCE_S)),
        flush=True,
    )

    await stop.wait()
    LOGGER.info("停机：观察器为文件源（守护线程随进程退出），断开 BLE")
    await central.stop()
    publisher.cancel()
    try:
        await publisher
    except asyncio.CancelledError:
        pass
    return 0


def run_serve(args: argparse.Namespace) -> int:
    try:
        code = asyncio.run(amain(args))
    except KeyboardInterrupt:
        code = 0
    return code


# ---------------------------------------------------------------------------
# CLI（观察器侧参数与 serve_zcode 同口径；无 WSS/TLS/token 项——BLE 配对即安全）
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge_serve_ble",
        description="CodexDT serve-ble: push ZCode-observed AppState snapshots "
                    "to the device over BLE (Mac as Bleak central, ZC10)",
    )
    p.add_argument("--device-name", default="CodexDT",
                   help="设备广播名过滤（§2.2：仅连广播含冻结服务 UUID 的设备；"
                        "默认 CodexDT）")
    p.add_argument("--address", default=None,
                   help="设备 MAC/UUID 地址过滤（可选；显式指定时优先于名称）")
    p.add_argument("--scan-timeout", type=float, default=10.0,
                   help="单轮扫描超时秒（默认 10；超时按退避序列重扫）")
    p.add_argument("--rollout-dir", default=None,
                   help="ZCode rollout 目录（默认 ~/.zcode/cli/rollout，由观察器定）")
    p.add_argument("--agents-dir", default=None,
                   help="ZCode agents 目录（默认 ~/.zcode/cli/agents，由观察器定）")
    p.add_argument("--spool", default=None,
                   help="hook spool 路径（默认 CDT_HOOK_SPOOL 或 "
                        "~/.zcode/cli/cdt-hook-spool.jsonl，与写入端同源）")
    p.add_argument("--poll-interval", type=float, default=1.0,
                   help="观察器轮询间隔秒（默认 1.0）")
    p.add_argument("--epoch", default=None,
                   help="bridge_epoch（默认 zcode-<启动毫秒>，每次启动新 ID）")
    p.add_argument("--no-observer", action="store_true",
                   help="仅启动 BLE central（不接观察器；连接级冒烟用）")
    p.add_argument("--dry-run", action="store_true",
                   help="不扫描不连接：打印名称/UUID 与流程摘要后退出 0")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.dry_run:
        return run_dry_run(args)
    return run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
