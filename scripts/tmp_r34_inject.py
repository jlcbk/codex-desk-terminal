#!/usr/bin/env python3
"""tmp_r34_inject.py — 烧录验证轮（A3+A4）临时注入工具，随轮收编后可删。

在设备配置的 Bridge 端点（TLS-terminated，同 bridge_serve_mock 架构）上按
测试剧本注入快照流，用于 R4 重同步/优先槽真机验证：

  silent     接受连接、按协议自动 pong（websockets 库行为），但永不推送任何
             快照（provider=None + keepalive 关）——制造「TCP/TLS 健康但业务
             停摆」，命中设备侧 R4 新鲜度重同步触发域（INTERFACES §4/§6）。

  alternate  从 mock lifecycle 场景取 needs_you / done 两份快照交替注入：
             A) needs_you 后 10ms 跟 done（同拍排队 → 优先槽先出的呈现顺序）
             B) 连发 N 份 needs_you（间隔 25ms → 消费/渲染期间覆盖优先槽，
             dropped_priority 必须计数）后跟一份 done。每拍 seq 严格递增、
             epoch 用运行期 ID（§3 语义不变）。

token 只经 --token-file 读取，不进日志（红线同 bridge_serve_mock）。
用法示例见 README 或本轮验证报告。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from bridge.sources import mock  # noqa: E402
from bridge.transports.wss.server import WssServer, WssServerConfig  # noqa: E402
import bridge_serve_mock as bsm  # noqa: E402  （复用其阻塞式 TLS 终结线程）

LOGGER = logging.getLogger("cdt.tmp_r34_inject")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="R4 verify snapshot injector (temp tool)")
    p.add_argument("mode", choices=["silent", "alternate"])
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cert", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--token-file", default="config/local/device_token")
    p.add_argument("--scenario", default=mock.SCENARIO_LIFECYCLE)
    p.add_argument("--burst", type=int, default=6, help="alternate B 拍 needs_you 连发份数")
    p.add_argument("--gap-ms", type=int, default=25, help="连发间隔 ms")
    p.add_argument("--phases", type=int, default=2, help="A/B 拍重复轮数")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


class SeqRenderer:
    """epoch 内全局严格递增 seq 的快照渲染（同 MockScenarioSource._render 语义；
    单计数器跨 needs_you/done 共用——设备按 §3 丢弃 ≤已应用的 seq）。"""

    def __init__(self, epoch: str) -> None:
        self._epoch = epoch
        self._seq = 0

    def render(self, base: dict) -> bytes:
        self._seq += 1
        snap = copy.deepcopy(base)
        snap["seq"] = self._seq
        snap["bridge_epoch"] = self._epoch
        return json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @property
    def seq(self) -> int:
        return self._seq


async def run_silent(args: argparse.Namespace, token: str) -> None:
    cfg = WssServerConfig(
        host="127.0.0.1", port=args.port + 1, max_clients=1,
        keepalive_interval_s=None,  # 关存活推送：业务完全静默
        tls_cert_file=None, tls_key_file=None, allow_insecure_loopback=True,
    )
    server = WssServer(cfg, device_token=token, snapshot_provider=None)
    await server.start()
    threading.Thread(target=bsm._tls_thread_server,
                     args=(args, args.port + 1, args.host, args.port),
                     daemon=True, name="tls-term").start()
    print(f"tmp_r34_inject[silent]: serving wss://{args.host}:{args.port}/v1/state "
          f"(no push, pong only)", flush=True)
    await asyncio.Event().wait()  # 由 SIGTERM 结束


async def run_alternate(args: argparse.Namespace, token: str) -> None:
    snaps = mock.run(args.scenario, epoch="inject")

    def pick(state: str) -> dict:
        return next(s for s in snaps
                    if s.get("threads") and s["threads"][0].get("state") == state)

    needs_you = pick("needs_you")
    done = pick("done")
    epoch = f"mock-inject-{int(time.time() * 1000)}"
    r = SeqRenderer(epoch)

    cfg = WssServerConfig(
        host="127.0.0.1", port=args.port + 1, max_clients=1,
        keepalive_interval_s=None,
        tls_cert_file=None, tls_key_file=None, allow_insecure_loopback=True,
    )
    server = WssServer(cfg, device_token=token, snapshot_provider=None)
    await server.start()
    threading.Thread(target=bsm._tls_thread_server,
                     args=(args, args.port + 1, args.host, args.port),
                     daemon=True, name="tls-term").start()
    print(f"tmp_r34_inject[alternate]: serving wss://{args.host}:{args.port}/v1/state "
          f"epoch={epoch} burst={args.burst} gap={args.gap_ms}ms phases={args.phases}",
          flush=True)
    await asyncio.sleep(0.3)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    round_no = 0
    while not stop.is_set():
        round_no += 1
        await asyncio.sleep(4.0)  # 等设备在位（重连完成后注入才有效）
        if stop.is_set() or server.client_count == 0:
            LOGGER.info("round %d: 无设备连接，等待", round_no)
            continue
        # A 拍：needs_you 紧跟 done（10ms）→ 双槽同拍排队，优先槽先出
        you = r.render(needs_you)
        seq_you = r.seq
        await server.send(you)
        await asyncio.sleep(0.010)
        await server.send(r.render(done))
        LOGGER.info("round %d A拍: you(seq=%d) done(seq=%d)", round_no, seq_you, r.seq)
        await asyncio.sleep(3.0)
        # B 拍：连发 needs_you → 覆盖未消费提醒，dropped_priority 必须计数
        first = r.seq
        for _ in range(args.burst):
            await server.send(r.render(needs_you))
            await asyncio.sleep(args.gap_ms / 1000.0)
        last = r.seq
        await asyncio.sleep(2.0)
        await server.send(r.render(done))
        LOGGER.info("round %d B拍: you×%d(seq %d..%d) done(seq=%d)",
                    round_no, args.burst, first + 1, last, r.seq)
        if round_no >= args.phases:
            LOGGER.info("注入拍已完成（保持连接、静默），等 SIGTERM")
            await stop.wait()
    await server.stop()


async def amain() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("token 文件为空", file=sys.stderr)
        return 2
    if args.mode == "silent":
        await run_silent(args, token)
    else:
        await run_alternate(args, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
