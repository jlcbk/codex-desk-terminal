#!/usr/bin/env python3
"""bridge_serve_mock.py — LAN 模式 Bridge 入口（整机集成 v1，A3+A4 合并）。

任务书：scripts/run_bridge_lan.sh 的快照源适配器——把 mock 场景快照经
bridge/transports/wss/server.py 的 snapshot_provider/send 接口推送到设备。

设计（契约真源：docs/INTERFACES.md §3/§4、protocol/transport.md §5）：
  - 快照来源：bridge.sources.mock.run(scenario)（--source mock --scenario lifecycle
    的同源生成器，每事件一份快照）。
  - 循环播放：每 event_period_s 推进并推送一份快照；场景播完后 hold_s 秒保持
    末帧，再从头循环。
  - seq 单调：同一 epoch 内所有实际发送（播放 + 15s 存活心跳）共享一个严格
    递增计数器——场景每轮循环 seq 继续递增，绝不回卷（INTERFACES §3：
    同 epoch ≤ 已应用值丢弃；存活快照也占 seq）。
  - 存活心跳（§4 默认 15s）：交给 WssServer 内建 keepalive_loop——provider
    返回"当前快照 + 新 seq"的字节，字节变化才会真正发送，等价于每 15s 一份
    全量存活快照。
  - bridge_epoch 默认每次启动新生成（INTERFACES §3：Bridge 每次启动生成新 ID），
    设备重连后按新 epoch 全量替换。
  - TLS：LAN 模式必须 wss（transport.md §5.1 生产禁明文）；loopback 开发允许
    显式 --allow-insecure-loopback 降级。
  - 凭证红线：token 只以 SHA-256 指纹前 8 hex 进日志（server.py 已实现），
    上行遥测只记字节数不读内容。

用法（一般经 scripts/run_bridge_lan.sh 调起）：
  uv run --python 3.12 --with 'websockets==17.1' \
    python3 scripts/bridge_serve_mock.py \
    --host 192.168.1.124 --port 8765 --scenario lifecycle \
    --cert config/local/dev-certs/server.crt --key config/local/dev-certs/server.key \
    --token-file config/local/device_token
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.sources import mock  # noqa: E402
from bridge.transports.wss.server import (  # noqa: E402
    SendStatus,
    WssServer,
    WssServerConfig,
)

LOGGER = logging.getLogger("cdt.integration.bridge_serve_mock")


class MockScenarioSource:
    """mock 场景快照源：播放指针 + epoch 内单调 seq 计数器（单事件循环线程）。"""

    def __init__(self, snapshots: list[dict], epoch: str) -> None:
        self._snapshots = snapshots
        self._epoch = epoch
        self._seq = 0
        self._idx = 0

    @property
    def index(self) -> int:
        return self._idx

    @property
    def seq(self) -> int:
        return self._seq

    def _render(self, idx: int) -> bytes:
        """渲染第 idx 份快照：写运行期 epoch + 新 seq（§3：Bridge 每次启动生成
        新 epoch；同 epoch 内 seq 严格递增——重启后设备按新 epoch 全量替换）。"""
        snap = copy.deepcopy(self._snapshots[idx])
        self._seq += 1
        snap["seq"] = self._seq
        snap["bridge_epoch"] = self._epoch
        return json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _current_idx(self) -> int:
        """播放指针夹到有效范围（播放推进后 idx==len，keepalive 仍取末帧）。"""
        return min(self._idx, len(self._snapshots) - 1)

    async def next_playback(self) -> bytes:
        """播放推进一份快照（场景末尾由调用方控制 hold/回卷）。"""
        data = self._render(self._idx)
        LOGGER.info(
            "playback seq=%d idx=%d/%d bytes=%d",
            self._seq, self._idx + 1, len(self._snapshots), len(data),
        )
        self._idx += 1
        return data

    async def keepalive(self) -> bytes:
        """存活快照：不推进播放，但 seq 递增（§4 存活快照也占 seq）。

        注意：server 以 `await provider()` 调用（SnapshotProvider 协议），
        必须是协程函数——同步实现会在 keepalive_loop 里抛 TypeError。
        """
        idx = self._current_idx()
        data = self._render(idx)
        LOGGER.info(
            "keepalive seq=%d idx=%d/%d bytes=%d",
            self._seq, idx + 1, len(self._snapshots), len(data),
        )
        return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CodexDT Bridge LAN entry: mock scenario snapshots over WSS "
                    "(protocol/transport.md §5 frozen semantics)")
    p.add_argument("--host", required=True, help="监听地址（LAN 模式=显式 LAN IP，不绑 0.0.0.0）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cert", default=None, help="TLS 证书（LAN 模式必须；loopback dev 可省）")
    p.add_argument("--key", default=None, help="TLS 私钥（与 --cert 成对）")
    p.add_argument("--tls-terminate", dest="tls_terminate", action="store_true", default=True,
                   help="LAN TLS 由本进程 asyncio 终结（实测 ESP32 兼容），明文仅转发到 "
                        "127.0.0.1 回环内的 websockets server（默认开）")
    p.add_argument("--no-tls-terminate", dest="tls_terminate", action="store_false",
                   help="TLS 直接由 websockets 处理（默认关闭路径；当前 asyncio 组合下"
                        "对 ESP32 TLS1.2 客户端存在握手后 EOF 兼容问题，见集成报告）")
    p.add_argument("--token-file", default="config/local/device_token",
                   help="设备 token 文件（不入 Git/日志）")
    p.add_argument("--scenario", default=mock.SCENARIO_LIFECYCLE,
                   help="mock 场景名（默认 lifecycle）；可选: " + ", ".join(mock.SCENARIO_NAMES))
    p.add_argument("--epoch", default=None,
                   help="bridge_epoch（默认 mock-lan-<启动毫秒>，每次启动新 ID）")
    p.add_argument("--event-period-s", type=float, default=4.0,
                   help="播放节奏：每份快照间隔秒（默认 4.0）")
    p.add_argument("--hold-s", type=float, default=30.0,
                   help="场景播完后保持末帧秒数，再回卷循环（默认 30.0）")
    p.add_argument("--keepalive-interval-s", type=float, default=15.0,
                   help="存活快照间隔秒（INTERFACES §4 默认 15；0=禁用）")
    p.add_argument("--max-clients", type=int, default=1)
    p.add_argument("--allow-insecure-loopback", action="store_true",
                   help="开发 loopback 明文 ws://（仅 127.0.0.1/::1/localhost；§5.1）")
    p.add_argument("--no-loop", action="store_true", help="场景播完即停（默认循环）")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    snapshots = mock.run(args.scenario, epoch=args.epoch or "mock-lan-planning")
    # 真正下发时逐份重写 seq/epoch：epoch 用运行期 ID（每次启动新 ID，§3）
    epoch = args.epoch or f"mock-lan-{int(time.time() * 1000)}"
    source = MockScenarioSource(snapshots, epoch)
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print(f"bridge_serve_mock: token 文件为空: {args.token_file}", file=sys.stderr)
        return 2

    keepalive_s = None if args.keepalive_interval_s <= 0 else args.keepalive_interval_s

    # TLS 终结模式（默认）：websockets server 只监听 127.0.0.1 明文（§5.1 dev
    # 标记），LAN 侧由本进程的 asyncio TLS server 终结后逐字节转发。
    # 依据（2026-09-11 真机联调）：websockets/asyncio TLS 对 ESP32 TLS1.2 客户端
    # 存在"握手完成后 HTTP 读 EOF"兼容问题（证据
    # artifacts/board/integration/tls_probe.log：同一设备同一证书裸 ssl 服务
    # 正常），逐字节转发对 WebSocket 帧/心跳完全透明，冻结语义不变。
    serve_host, serve_port = args.host, args.port
    use_terminate = bool(args.cert) and args.tls_terminate
    if use_terminate:
        serve_host = "127.0.0.1"
        serve_port = args.port + 1

    cfg = WssServerConfig(
        host=serve_host,
        port=serve_port,
        max_clients=args.max_clients,
        keepalive_interval_s=keepalive_s,
        tls_cert_file=None if use_terminate else args.cert,
        tls_key_file=None if use_terminate else args.key,
        allow_insecure_loopback=True if use_terminate else args.allow_insecure_loopback,
    )

    def on_link(status: str, detail: object) -> None:
        LOGGER.info("link %s detail=%s clients=%d", status, detail, server.client_count)

    def on_message(data: bytes, length: int) -> None:
        # 上行遥测：只记字节数（红线：不读取内容）
        LOGGER.info("uplink telemetry bytes=%d", length)

    server = WssServer(cfg, device_token=token,
                       snapshot_provider=source.keepalive,
                       on_link=on_link, on_message=on_message)
    await server.start()

    tls_thread = None
    if use_terminate:
        tls_thread = threading.Thread(
            target=_tls_thread_server,
            args=(args, serve_port, args.host, args.port),
            daemon=True, name="cdt-tls-terminator")
        tls_thread.start()

    scheme = "wss(TLS-terminated)" if args.cert else "ws(dev-loopback)"
    print(
        f"bridge_serve_mock: serving {scheme}://{args.host}:{args.port}/v1/state "
        f"scenario={args.scenario} epoch={epoch} snapshots={len(snapshots)} "
        f"event_period={args.event_period_s}s hold={args.hold_s}s "
        f"keepalive={keepalive_s}s",
        flush=True,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # 非 POSIX
            pass

    async def playback() -> None:
        """播放任务：event_period_s 推一份；末帧 hold_s 后回卷（seq 继续递增）。"""
        await asyncio.sleep(0.2)  # 让 start 日志先行
        await server.send(await source.next_playback())
        while not stop.is_set():
            if source.index >= len(snapshots):
                if args.no_loop:
                    LOGGER.info("场景播完（--no-loop），保持末帧")
                    await stop.wait()
                    break
                await asyncio.sleep(args.hold_s)
                source._idx = 0  # 回卷播放指针；seq 由 _render 继续单调递增
                LOGGER.info("场景回卷（epoch=%s seq=%d 继续递增）", epoch, source.seq)
            else:
                await asyncio.sleep(args.event_period_s)
            if stop.is_set():
                break
            status = await server.send(await source.next_playback())
            if status is SendStatus.ERROR:
                LOGGER.error("快照被拒（超上限），等待下一拍")

    player = asyncio.create_task(playback(), name="cdt-mock-playback")
    await stop.wait()
    player.cancel()
    try:
        await player
    except asyncio.CancelledError:
        pass
    LOGGER.info("停机：close 1000 给全部连接（§5.3 Bridge 正常关闭）")
    await server.stop()
    return 0


def build_tls_context(args: argparse.Namespace):
    """LAN TLS 上下文（终结模式）：与 websockets/tlsutil 相同的证书/密钥，
    显式 TLS1.2 起步。设备侧 CA 验链 + SPKI pinning 语义不变。"""
    import ssl as _ssl
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    return ctx


def _pump(src, dst, tag=""):
    n = 0
    try:
        while True:
            data = src.recv(16384)
            if not data:
                LOGGER.info("pump%s EOF (total %d bytes)", tag, n)
                break
            n += len(data)
            LOGGER.info("pump%s + %d bytes %r", tag, len(data), data[:64])
            dst.sendall(data)
    except OSError as exc:
        LOGGER.info("pump%s OSError %s (total %d bytes)", tag, exc, n)
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _tls_thread_server(args: argparse.Namespace, up_port: int,
                       host: str, port: int) -> None:
    """阻塞式 TLS 终结线程（accept→wrap→与回环明文 websockets 双向转发）。

    为什么不用 asyncio TLS（2026-09-11 真机结论）：asyncio（websockets 内建
    与 asyncio.start_server 两种）对 ESP32/mbedTLS TLS1.2 客户端存在握手卡死
    ~10s（对端超时）的兼容问题；阻塞式 socket + 同一证书握手 1.1s 正常
    （证据：artifacts/board/integration/tls_probe.log）。转发为逐字节透明，
    WebSocket 帧/心跳语义不变；明文仅存在于本机回环（§5.1 dev 标记）。"""
    import ssl as _ssl
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((host, port))
    lsock.listen(4)
    LOGGER.info("TLS terminator (blocking) listening on %s:%d -> 127.0.0.1:%d",
                host, port, up_port)
    while True:
        try:
            raw, peer = lsock.accept()
        except OSError as exc:
            LOGGER.error("TLS accept 失败: %s", exc)
            return
        LOGGER.info("tls-relay accept %s", peer)

        def handle(raw_sock, peer_addr):
            try:
                raw_sock.settimeout(15)
                tls = ctx.wrap_socket(raw_sock, server_side=True)
                LOGGER.info("tls-relay %s handshake ok (%s)", peer_addr, tls.version())
                tls.settimeout(None)
                up = socket.create_connection(("127.0.0.1", up_port), timeout=10)
                LOGGER.info("tls-relay %s upstream connected", peer_addr)
                t = threading.Thread(target=_pump, args=(tls, up, f"(tls>{peer_addr})"), daemon=True)
                t.start()
                _pump(up, tls, f"(up>{peer_addr})")
                t.join(timeout=1)
            except Exception as exc:  # noqa: BLE001 — 单连接失败不影响服务
                LOGGER.info("tls-relay %s error: %s: %s",
                            peer_addr, type(exc).__name__, exc)
            finally:
                LOGGER.info("tls-relay %s closed", peer_addr)
                try:
                    raw_sock.close()
                except OSError:
                    pass

        threading.Thread(target=handle, args=(raw, peer), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
