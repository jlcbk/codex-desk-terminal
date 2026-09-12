#!/usr/bin/env python3
"""bridge_serve_zcode.py — ZCode 观察源（source.kind=zcode_observed）的持续 WSS
显示服务（ZC2，A2）。第三条服务先例：mock（bridge_serve_mock.py）、真实 codex
（bridge_serve_codex.py）之后，把 ZC1 的 ZcodeObserver 接成常驻服务。

结构（复用 bridge/transports/wss/server.py + bridge/serve_codex.py 的服务编排）：
  StateEngine(epoch="zcode-<启动毫秒>", source_kind=SOURCE_ZCODE_OBSERVED,
              utc_anchor_ms=<wall-mono 固定锚点>)
  + ZcodeObserver（run_forever 每份快照回调 sink）
  → ServeHub 盖章 wire epoch/seq → 有界发布桥（单消费者保帧序）
  → WssServer 广播给所有已连接设备订户（TLS 证书/token 与现有服务一致）。

epoch/重连语义：观察器基于文件，无连接概念；目录消失由 ZC1 的
source_disconnected 事件体现，服务层不做额外动作。服务每次启动生成新 epoch
（设备按新 epoch 全量替换，INTERFACES §3）。

契约注意：
  - ZcodeObserver 的 import 放在观察器线程内延迟执行（ZC1 模块可能尚未落地，
    本脚本的 --help / --dry-run 不得因此起不来）。
  - --spool 默认与 hook 写入端同源：CDT_HOOK_SPOOL 环境变量优先，否则
    ~/.zcode/cli/cdt-hook-spool.jsonl（读写两端对齐，见 zcode_hook_spool.py）。
  - token 只进内存与指纹日志；上行遥测只记字节数（红线同现有服务）。

用法：
  # 冒烟（无设备环境：只打印路径/参数摘要，退出 0）
  python3 scripts/bridge_serve_zcode.py --dry-run
  # 开发 loopback（明文 ws，显式 dev 标记）
  uv run --python 3.12 --with 'websockets==17.1' python3 scripts/bridge_serve_zcode.py \
    --allow-insecure-loopback --port 8765
  # LAN wss（TLS 证书默认 config/local/dev-certs/server.{crt,key}）
  uv run --python 3.12 --with 'websockets==17.1' python3 scripts/bridge_serve_zcode.py \
    --host <LAN-IP> --port 8765 --token-file config/local/device_token
"""

from __future__ import annotations

import argparse
import asyncio
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
from bridge.state import reducer as rd  # noqa: E402
from bridge.state import render as render_mod  # noqa: E402
from bridge.state.engine import SOURCE_ZCODE_OBSERVED, StateEngine  # noqa: E402

LOGGER = logging.getLogger("cdt.serve_zcode")

# ---- 默认值 -----------------------------------------------------------------

DEFAULT_CERTS_DIR = "config/local/dev-certs"
DEFAULT_TOKEN_FILE = "config/local/device_token"
DEFAULT_ROLLOUT_DIR = "~/.zcode/cli/rollout"   # 展示用；观察器内为同一默认
DEFAULT_AGENTS_DIR = "~/.zcode/cli/agents"     # 展示用；观察器内为同一默认
DEFAULT_SPOOL = "~/.zcode/cli/cdt-hook-spool.jsonl"  # 与 zcode_hook_spool.py 对齐

#: 发布桥（观察器线程 → 事件循环单消费者，保证帧序）容量。
PUBLISH_QUEUE_MAX = 256


# ---------------------------------------------------------------------------
# 纯逻辑（不依赖 websockets / ZC1 模块，可离线单测）
# ---------------------------------------------------------------------------

def resolve_spool_path(environ: dict | None = None) -> str:
    """服务端 spool 默认：CDT_HOOK_SPOOL 优先（与 hook 写入端同源对齐）。"""
    env = os.environ if environ is None else environ
    return env.get("CDT_HOOK_SPOOL") or DEFAULT_SPOOL


def initial_snapshot(epoch: str, anchor_ms: int, now_mono: int = 0) -> dict:
    """空闲初始快照：threads 空（纯 IDLE）、usage 未可用、source connected。

    用空内部状态直接渲染（不经事件：空闲没有任何上游事实可报）；
    source_kind=zcode_observed（契约 v1.1）。"""
    return render_mod.render(
        rd.new_internal_state(),
        bridge_epoch=epoch, seq=0, source_kind=SOURCE_ZCODE_OBSERVED,
        now_mono=int(now_mono), anchor_ms=int(anchor_ms))


def resolve_tls(args: argparse.Namespace) -> tuple:
    """TLS 证书/私钥解析：显式 --cert/--key 优先；否则 --certs-dir 下的
    server.{crt,key}；--allow-insecure-loopback 显式降级为 loopback 明文。
    返回 (cert, key, 使用明文 bool)。"""
    if args.cert or args.key:
        if not (args.cert and args.key):
            raise ValueError("--cert 与 --key 必须成对给出")
        return args.cert, args.key, False
    if args.allow_insecure_loopback:
        return None, None, True
    cert = Path(args.certs_dir) / "server.crt"
    key = Path(args.certs_dir) / "server.key"
    if cert.is_file() and key.is_file():
        return str(cert), str(key), False
    return None, None, False


def run_dry_run(args: argparse.Namespace) -> int:
    """--dry-run：不起 WSS、不构造观察器（不 import websockets / ZC1 模块），
    打印将用的路径/参数摘要（含各默认路径是否存在），退出 0。"""
    epoch = args.epoch or ("zcode-%d" % int(time.time() * 1000))
    cert, key, plaintext = (None, None, True)
    if not args.allow_insecure_loopback:
        cert = Path(args.certs_dir) / "server.crt"
        key = Path(args.certs_dir) / "server.key"
    spool = Path(args.spool).expanduser() if args.spool else Path(
        resolve_spool_path()).expanduser()
    rollout = Path(args.rollout_dir).expanduser() if args.rollout_dir else Path(
        DEFAULT_ROLLOUT_DIR).expanduser()
    agents = Path(args.agents_dir).expanduser() if args.agents_dir else Path(
        DEFAULT_AGENTS_DIR).expanduser()
    token_file = Path(args.token_file)

    def _exists(p: Path) -> str:
        return "存在" if p.exists() else "缺失"

    print("bridge_serve_zcode --dry-run（不起 WSS、不构造观察器）")
    print("source_kind: %s" % SOURCE_ZCODE_OBSERVED)
    print("epoch: %s" % epoch)
    print("rollout_dir: %s (%s)" % (rollout, _exists(rollout)))
    print("agents_dir: %s (%s)" % (agents, _exists(agents)))
    print("spool: %s (%s; 来源=%s)" % (
        spool, _exists(spool),
        "CDT_HOOK_SPOOL" if (not args.spool and os.environ.get("CDT_HOOK_SPOOL"))
        else ("--spool" if args.spool else "默认")))
    print("poll_interval: %ss" % args.poll_interval)
    scheme = "ws(dev-loopback 明文)" if args.allow_insecure_loopback else "wss"
    print("listen: %s://%s:%d/v1/state" % (scheme, args.host, args.port))
    if args.allow_insecure_loopback:
        print("tls: 明文（--allow-insecure-loopback 显式 dev 标记；仅限 loopback）")
    else:
        print("tls: cert=%s (%s) key=%s (%s)" % (
            cert, _exists(cert) if cert else "-",
            key, _exists(key) if key else "-"))
    print("token_file: %s (%s；内容不读取打印)" % (token_file, _exists(token_file)))
    print("keepalive: %s" % ("%ss" % args.keepalive_interval_s
                             if args.keepalive_interval_s > 0 else "禁用"))
    print("max_clients: %d" % args.max_clients)
    print("spool_writer: %s" % (REPO_ROOT / "scripts" / "zcode_hook_spool.py"))
    print("dry-run PASS（退出 0；真实服务需观察器 bridge/sources/zcode.py 已落地）")
    return 0


# ---------------------------------------------------------------------------
# serve：观察器线程 + asyncio 接线
# ---------------------------------------------------------------------------

class PublishBridge:
    """观察器线程 → 事件循环的发布桥（单消费者按序推送，帧序保证）。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.dropped = 0  # 发布桥溢出计数（Bridge 缺陷，应为 0）

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue = asyncio.Queue(maxsize=PUBLISH_QUEUE_MAX)

    def submit(self, data: bytes) -> None:
        def _put():
            try:
                self.queue.put_nowait(data)
            except asyncio.QueueFull:
                self.dropped += 1
                LOGGER.error("发布桥满（%d），丢弃快照 seq（Bridge 缺陷）",
                             self.dropped)
        try:
            self.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            LOGGER.warning("事件循环已关闭，快照未发布（停机中）")


def make_sink(hub: ServeHub, bridge: PublishBridge):
    """观察器 sink：每份快照盖章 wire epoch/seq（ServeHub 编排，与
    bridge_serve_codex 一致）后交发布桥广播给全部已连接订户。"""
    def sink(snapshot: dict) -> None:
        try:
            data = hub.publish(snapshot)
        except Exception as exc:  # noqa: BLE001 — 单份坏快照不拖垮观察器线程
            LOGGER.exception("快照盖章失败 %s: %s", type(exc).__name__, exc)
            return
        LOGGER.info("快照发布 seq=%d epoch=%s bytes=%d",
                    hub.seq, hub.epoch, len(data))
        bridge.submit(data)
    return sink


def observer_worker(args: argparse.Namespace, engine: StateEngine, sink,
                    spool_path: str) -> None:
    """观察器线程：ZcodeObserver 延迟导入（ZC1 未落地时本脚本其余功能可用）。

    run_forever 阻塞常驻；停机 = 进程退出（文件源无连接语义，守护线程）。
    rollout/agents 未显式给出时传 None，由观察器使用其冻结默认路径。"""
    from bridge.sources.zcode import ZcodeObserver  # noqa: 延迟导入（ZC1）

    obs = ZcodeObserver(
        engine,
        rollout_dir=args.rollout_dir,
        agents_dir=args.agents_dir,
        spool_path=spool_path,
        poll_interval_s=args.poll_interval,
    )
    obs.run_forever(sink, monotonic_fn=time.monotonic)


async def _publisher_loop(queue: asyncio.Queue, server) -> None:
    """单消费者按序推送；send 三值结果只记 ERROR（超 16KiB = Bridge 缺陷）。"""
    from bridge.transports.wss.server import SendStatus

    while True:
        data = await queue.get()
        status = await server.send(data)
        if status is SendStatus.ERROR:
            LOGGER.error("快照被拒（超 16KiB 上限，Bridge 缺陷）")


async def amain(args: argparse.Namespace) -> int:
    from bridge.transports.wss.server import WssServer, WssServerConfig

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("bridge_serve_zcode: token 文件为空: %s" % args.token_file,
              file=sys.stderr)
        return 2

    try:
        cert, key, plaintext = resolve_tls(args)
    except ValueError as exc:
        print("bridge_serve_zcode: %s" % exc, file=sys.stderr)
        return 2
    if not plaintext and cert is None:
        print("bridge_serve_zcode: TLS 证书缺失（%s/server.{crt,key}）；"
              "先运行 sh scripts/gen_dev_certs.sh，或开发 loopback 显式 "
              "--allow-insecure-loopback" % args.certs_dir, file=sys.stderr)
        return 2

    wall_ms = int(time.time() * 1000)
    mono_ms = int(time.monotonic() * 1000)
    epoch = args.epoch or ("zcode-%d" % wall_ms)
    anchor_ms = wall_ms - mono_ms  # 固定锚点（确定性输出）
    engine = StateEngine(epoch, source_kind=SOURCE_ZCODE_OBSERVED,
                         utc_anchor_ms=anchor_ms)
    hub = ServeHub(base_epoch=epoch)  # 盖章层：同 epoch 字符串，字节中立
    bridge = PublishBridge()
    spool_path = str(Path(args.spool).expanduser()) if args.spool \
        else resolve_spool_path()

    keepalive_s = None if args.keepalive_interval_s <= 0 else args.keepalive_interval_s
    cfg = WssServerConfig(
        host=args.host,
        port=args.port,
        max_clients=args.max_clients,
        keepalive_interval_s=keepalive_s,
        tls_cert_file=cert,
        tls_key_file=key,
        # 红线（§5.1）：明文 ws:// 仅限 loopback 且必须显式标记（validate 兜底）
        allow_insecure_loopback=bool(plaintext),
    )
    server = WssServer(
        cfg, device_token=token,
        snapshot_provider=_keepalive_provider(hub),
        on_link=lambda status, detail: LOGGER.info(
            "link %s detail=%s clients=%d", status, detail, server.client_count),
        on_message=lambda data, n: LOGGER.info("uplink telemetry bytes=%d", n),
    )
    await server.start()

    # 初始 IDLE 存活快照（threads 空）：首个订户连接即得全量。
    bridge.start(asyncio.get_running_loop())
    initial_data = hub.publish(initial_snapshot(hub.epoch, anchor_ms, mono_ms))
    publisher = asyncio.create_task(_publisher_loop(bridge.queue, server),
                                    name="cdt-serve-zcode-publisher")
    bridge.submit(initial_data)

    observer = threading.Thread(
        target=observer_worker, args=(args, engine, make_sink(hub, bridge),
                                      spool_path),
        name="cdt-serve-zcode-observer", daemon=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # 非 POSIX
            pass

    observer.start()
    scheme = "ws(dev-loopback)" if plaintext else "wss"
    print(
        "bridge_serve_zcode: serving %s://%s:%d/v1/state epoch=%s "
        "source=%s rollout=%s agents=%s spool=%s poll=%ss keepalive=%ss "
        "clients=%d" % (
            scheme, args.host, server.bound_port, epoch, SOURCE_ZCODE_OBSERVED,
            args.rollout_dir or ("默认(%s)" % DEFAULT_ROLLOUT_DIR),
            args.agents_dir or ("默认(%s)" % DEFAULT_AGENTS_DIR),
            spool_path, args.poll_interval, keepalive_s, args.max_clients),
        flush=True,
    )

    await stop.wait()
    LOGGER.info("停机：观察器为文件源（守护线程随进程退出），关闭 WSS")
    publisher.cancel()
    try:
        await publisher
    except asyncio.CancelledError:
        pass
    LOGGER.info("停机：close 1000 给全部连接（§5.3 Bridge 正常关闭）")
    await server.stop()
    return 0


async def _keepalive_provider(hub: ServeHub):
    """WssServer keepalive_loop 的 provider（必须为协程函数；末份内容+新 seq）。"""
    return hub.keepalive()


def run_serve(args: argparse.Namespace) -> int:
    try:
        code = asyncio.run(amain(args))
    except KeyboardInterrupt:
        code = 0
    return code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge_serve_zcode",
        description="CodexDT serve-zcode: persistent WSS service over the "
                    "ZCode session observer (source.kind=zcode_observed, ZC2)",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（LAN/wss=显式 LAN IP；明文仅限 loopback）")
    p.add_argument("--port", type=int, default=8765,
                   help="监听端口（默认 8765；与其它服务并存时显式换端口）")
    p.add_argument("--certs-dir", default=DEFAULT_CERTS_DIR,
                   help="TLS 证书目录（默认 config/local/dev-certs，取其 "
                        "server.crt/server.key）")
    p.add_argument("--cert", default=None, help="显式 TLS 证书（覆盖 --certs-dir）")
    p.add_argument("--key", default=None, help="显式 TLS 私钥（与 --cert 成对）")
    p.add_argument("--allow-insecure-loopback", action="store_true",
                   help="开发 loopback 明文 ws://（仅 127.0.0.1/::1/localhost；§5.1）")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                   help="设备 token 文件（不入 Git/日志）")
    p.add_argument("--rollout-dir", default=None,
                   help="ZCode rollout 目录（默认 ~/.zcode/cli/rollout，由观察器定）")
    p.add_argument("--agents-dir", default=None,
                   help="ZCode agents 目录（默认 ~/.zcode/cli/agents，由观察器定）")
    p.add_argument("--spool", default=None,
                   help="hook spool 路径（默认 CDT_HOOK_SPOOL 或 "
                        "~/.zcode/cli/cdt-hook-spool.jsonl，与写入端同源）")
    p.add_argument("--poll-interval", type=float, default=1.0,
                   help="观察器轮询间隔秒（默认 1.0）")
    p.add_argument("--keepalive-interval-s", type=float, default=15.0,
                   help="存活快照间隔秒（INTERFACES §4 默认 15；0=禁用）")
    p.add_argument("--max-clients", type=int, default=1)
    p.add_argument("--epoch", default=None,
                   help="bridge_epoch（默认 zcode-<启动毫秒>，每次启动新 ID）")
    p.add_argument("--dry-run", action="store_true",
                   help="不起 WSS：打印将用的路径/参数摘要后退出 0（无设备冒烟）")
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
