#!/usr/bin/env python3
"""bridge_serve_codex.py — 真实 codex source 的持续 WSS 显示服务（R2，A1）。

ARCHITECTURE_REVIEW_2026-09-11 R2：P3.1 的 codex adapter 是批式收集器
（`python -m bridge --source codex --live` 一次性落快照），本脚本把它接成
常驻服务——基于 bridge/transports/wss server（接线方式同 bridge_serve_mock.py）
+ P3.1 CodexAdapter 服务钩子（bridge/sources/codex.py）。

形态：
  - 服务常驻；每个提交 → 一个 ephemeral 只读沙箱会话（P3.1 安全参数：
    固定模型 gpt-5.6-sol、readOnly、networkAccess=false、approvalPolicy=never、
    绝不 resume 已有 thread、审批只收不回），多任务串行（一次一个会话）。
  - 会话生命周期事件 → NormalizedEvent → StateEngine → 立即盖章 wire
    epoch/seq 推送 WSS（同 mock 服务的 §3/§4 语义；空闲推 threads 空的
    IDLE 存活快照，keepalive 每 15s 新 seq 全量下发）。
  - 配额流：启动读一次 + 每次 turn 终态后补读（rate_limits_after_turn）。
  - 进程退出不自动重跑 prompt（单次会话；R2：恢复只重同步，不重复执行任务）。

任务提交（两种都实现，见 bridge/serve_codex.py）：
  1) 命令行：python3 scripts/bridge_serve_codex.py submit --prompt "..."
     （经本地 unix socket 投递到常驻服务，不重建进程）
  2) 目录投递：向 --tasks-dir（默认 config/local/tasks）放入 *.prompt 文件，
     服务原子认领、处理后归档到 archive/（附 result.json）。

子命令：
  serve      启动常驻服务（默认子命令）
  submit     向常驻服务提交一个 prompt（unix socket）
  subscribe  轻量订户：连接 WSS 收快照流（验收/调试用，JSONL 落盘）

依赖锁：websockets==17.1（docs/VERSIONS.md）；Python 3.12（uv）。
运行（loopback 开发；LAN/wss 用 --cert/--key 直连 TLS——ESP32 端 TLS 终结
兼容路径属 mock/run_bridge_lan 链路，本服务验收为宿主订户，不做兼容宣称）：

  uv run --python 3.12 --with 'websockets==17.1' python3 scripts/bridge_serve_codex.py \
    serve --host 127.0.0.1 --port 18765 --allow-insecure-loopback \
    --socket-path config/local/bridge_serve_codex.sock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge import serve_codex  # noqa: E402
from bridge.serve_codex import (  # noqa: E402
    DEFAULT_EVIDENCE_DIR,
    DEFAULT_QUEUE_MAX,
    DEFAULT_SOCKET_PATH,
    DEFAULT_TASKS_DIR,
    RAW_LOG_MAX,
    SubmissionQueue,
    ServeHub,
    FileTaskSource,
    handle_socket_client,
    initial_snapshot,
    redaction_selfcheck,
)
from bridge.sources.codex import DEFAULT_MODEL, CodexAdapter  # noqa: E402

LOGGER = logging.getLogger("cdt.serve_codex")

# 发布桥（worker 线程 → 事件循环单消费者，保证帧序）容量。
PUBLISH_QUEUE_MAX = 256


# ---------------------------------------------------------------------------
# serve：worker（串行会话执行）
# ---------------------------------------------------------------------------

class ServeRuntime:
    """serve 子命令的共享运行态（worker 线程 / 事件循环两侧读写）。"""

    def __init__(self, args: argparse.Namespace, hub: ServeHub,
                 jobs: SubmissionQueue, file_source: FileTaskSource) -> None:
        self.args = args
        self.hub = hub
        self.jobs = jobs
        self.file_source = file_source
        self.stop = threading.Event()
        self.running_job = None       # worker 正在执行的 job_id（诊断用）
        self.jobs_done = 0
        self.dropped_frames = 0       # 发布桥溢出计数（Bridge 缺陷，应为 0）
        self.loop = None              # amain 里赋值
        self.publish_q = None

    # -- 快照发布（worker 线程调用） ----------------------------------------

    def _enqueue_publish(self, data: bytes) -> None:
        def _put():
            try:
                self.publish_q.put_nowait(data)
            except asyncio.QueueFull:
                self.dropped_frames += 1
                LOGGER.error("发布桥满（%d），丢弃快照 seq（Bridge 缺陷）",
                             self.dropped_frames)
        try:
            self.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            LOGGER.warning("事件循环已关闭，快照未发布（停机中）")

    # -- 单个任务（一个 ephemeral 会话，串行） --------------------------------

    def run_job(self, job) -> None:
        args = self.args
        epoch = self.hub.session_epoch(job.job_id)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        evidence_dir = Path(args.evidence_dir) / ("%s-%s" % (stamp, job.job_id))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        snaps_path = evidence_dir / "snapshots.jsonl"
        LOGGER.info("job %s 开始 origin=%s prompt=<content len=%d> epoch=%s "
                    "queued_depth=%d", job.job_id, job.origin, len(job.prompt),
                    epoch, self.jobs.depth)
        fh = snaps_path.open("w", encoding="utf-8")
        sink_state = {"last": None, "count": 0}

        def sink(snapshot: dict) -> None:
            """adapter 服务钩子：每份快照落盘证据 + 即时盖章发布（R2 实时链）。"""
            sink_state["last"] = snapshot
            sink_state["count"] += 1
            fh.write(json.dumps(snapshot, ensure_ascii=False,
                                separators=(",", ":")) + "\n")
            fh.flush()
            self._enqueue_publish(self.hub.publish(snapshot))

        adapter = CodexAdapter(
            prompt=job.prompt,
            model=args.model or DEFAULT_MODEL,  # 0.152.0 拒账户默认模型，必须固定
            codex_bin=args.codex_bin,
            turn_timeout=args.turn_timeout, epoch=epoch,
            # 服务钩子（R2）：快照即时交付不累积；原始日志有界；
            # turn 后补读配额；backoff=() = 单次会话，进程退出不自动重跑 prompt。
            # server_argv 仅测试注入（假 app-server）；生产为 [codex_bin, app-server]。
            server_argv=getattr(args, "server_argv", None),
            snapshot_sink=sink, backoff=(), raw_log_max=RAW_LOG_MAX,
            rate_limits_after_turn=True,
            task_label="R2-serve", run_label=job.origin,
        )
        started = time.monotonic()
        try:
            result = adapter.run()
        finally:
            fh.close()
        elapsed = time.monotonic() - started

        report_path = evidence_dir / "report.json"
        raw_path = evidence_dir / "events_raw_redacted.jsonl"
        report_path.write_text(json.dumps(result.report, ensure_ascii=False,
                                          indent=2) + "\n", encoding="utf-8")
        with raw_path.open("w", encoding="utf-8") as rf:
            for entry in result.raw_log:
                rf.write(json.dumps(entry, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")

        attempts = result.report.get("attempts") or [{}]
        turn_status = attempts[-1].get("turn_status")
        last_snap = sink_state["last"]
        summary = {
            "job_id": job.job_id,
            "origin": job.origin,
            "prompt": "<content len=%d>" % len(job.prompt),
            "bridge_epoch": epoch,
            "exit_code": result.exit_code,
            "exit_reason": result.exit_reason,
            "turn_status": turn_status,
            "snapshot_count": sink_state["count"],
            "elapsed_s": round(elapsed, 2),
            "usage_windows": (last_snap or {}).get("usage", {}).get("windows"),
            "usage_available": (last_snap or {}).get("usage", {}).get("available"),
            "evidence_dir": str(evidence_dir),
        }

        scanned = [snaps_path, raw_path, report_path]
        violations = redaction_selfcheck(scanned)
        summary["redaction_selfcheck"] = "PASS" if not violations else violations
        if violations:
            LOGGER.error("job %s 脱敏自检失败：%s", job.job_id, violations)

        (evidence_dir / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

        if job.claimed_path:
            archived = self.file_source.archive_result(job, summary)
            if archived is not None:
                LOGGER.info("job %s 目录任务已归档：%s", job.job_id, archived)

        if not violations:
            LOGGER.info("job %s 脱敏自检 PASS（%d 文件）", job.job_id, len(scanned))

        LOGGER.info("job %s 结束 exit_code=%d reason=%s turn_status=%s "
                    "snapshots=%s elapsed=%.1fs",
                    job.job_id, result.exit_code, result.exit_reason,
                    turn_status, summary["snapshot_count"], elapsed)

    def worker_loop(self) -> None:
        """串行消费：一次一个 ephemeral 会话；停机在任务间生效。"""
        while not self.stop.is_set():
            job = self.jobs.get(timeout=0.5)
            if job is None:
                continue
            self.running_job = job.job_id
            try:
                self.run_job(job)
            except Exception as exc:  # noqa: BLE001 — 单任务失败不拖垮服务
                LOGGER.exception("job %s 服务层异常：%s: %s",
                                 job.job_id, type(exc).__name__, exc)
            finally:
                self.jobs.task_done()
                self.running_job = None
                self.jobs_done += 1


# ---------------------------------------------------------------------------
# serve：asyncio 接线
# ---------------------------------------------------------------------------

async def _publisher_loop(publish_q: asyncio.Queue, server) -> None:
    """单消费者按序推送（帧序保证；send 三值结果只记 ERROR）。"""
    from bridge.transports.wss.server import SendStatus

    while True:
        data = await publish_q.get()
        status = await server.send(data)
        if status is SendStatus.ERROR:
            LOGGER.error("快照被拒（超 16KiB 上限，Bridge 缺陷）")


async def _socket_handler(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter,
                          runtime: ServeRuntime) -> None:
    """unix socket 控制连接（协议实现见 bridge/serve_codex.py，可离线单测）。"""
    await handle_socket_client(reader, writer, jobs=runtime.jobs, hub=runtime.hub,
                               state=runtime)


async def _file_poll_loop(runtime: ServeRuntime, interval_s: float) -> None:
    while not runtime.stop.is_set():
        try:
            claimed = runtime.file_source.poll_once()
            if claimed:
                LOGGER.info("目录投递认领：%s", claimed)
        except Exception:  # noqa: BLE001 — 目录扫描失败不拖垮服务
            LOGGER.exception("目录投递扫描失败")
        await asyncio.sleep(interval_s)


async def amain(args: argparse.Namespace, holder: dict) -> int:
    from bridge.transports.wss.server import WssServer, WssServerConfig

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print(f"bridge_serve_codex: token 文件为空: {args.token_file}",
              file=sys.stderr)
        return 2

    wall_ms = int(time.time() * 1000)
    mono_ms = int(time.monotonic() * 1000)
    anchor_ms = wall_ms - mono_ms
    base_epoch = args.epoch or ("codex-serve-%d" % wall_ms)
    hub = ServeHub(base_epoch)
    jobs = SubmissionQueue(maxsize=args.queue_max)
    file_source = FileTaskSource(args.tasks_dir, jobs)
    file_source.prepare()
    runtime = ServeRuntime(args, hub, jobs, file_source)

    keepalive_s = None if args.keepalive_interval_s <= 0 else args.keepalive_interval_s
    cfg = WssServerConfig(
        host=args.host,
        port=args.port,
        max_clients=args.max_clients,
        keepalive_interval_s=keepalive_s,
        tls_cert_file=args.cert,
        tls_key_file=args.key,
        # 红线（§5.1）：明文 ws:// 仅限 loopback 且必须显式标记（validate 兜底）
        allow_insecure_loopback=bool(args.allow_insecure_loopback),
    )
    server = WssServer(
        cfg, device_token=token,
        snapshot_provider=lambda: _keepalive_provider(hub),
        on_link=lambda status, detail: LOGGER.info(
            "link %s detail=%s clients=%d", status, detail, server.client_count),
        on_message=lambda data, n: LOGGER.info("uplink telemetry bytes=%d", n),
    )
    await server.start()

    # 初始 IDLE 存活快照（threads 空）：首个订户连接即得全量。
    initial_data = hub.publish(initial_snapshot(hub.epoch, anchor_ms, mono_ms))
    runtime.loop = asyncio.get_running_loop()
    runtime.publish_q = asyncio.Queue(maxsize=PUBLISH_QUEUE_MAX)
    publisher = asyncio.create_task(_publisher_loop(runtime.publish_q, server),
                                    name="cdt-serve-codex-publisher")
    await runtime.publish_q.put(initial_data)

    socket_path = Path(args.socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()  # 残留 socket 文件（上次异常退出）
    unix_server = await asyncio.start_unix_server(
        lambda r, w: _socket_handler(r, w, runtime), path=str(socket_path))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # 非 POSIX
            pass

    worker = threading.Thread(target=runtime.worker_loop,
                              name="cdt-serve-codex-worker", daemon=False)
    worker.start()
    holder["worker"] = worker
    holder["runtime"] = runtime
    poller = None
    if args.tasks_dir:
        poller = asyncio.create_task(_file_poll_loop(runtime, args.poll_interval_s),
                                     name="cdt-serve-codex-filepoll")

    scheme = "wss" if args.cert else "ws(dev-loopback)"
    print(
        f"bridge_serve_codex: serving {scheme}://{args.host}:{server.bound_port}/v1/state "
        f"base_epoch={base_epoch} model={args.model} socket={socket_path} "
        f"tasks_dir={args.tasks_dir} evidence={args.evidence_dir} "
        f"queue_max={args.queue_max} keepalive={keepalive_s}s "
        f"turn_timeout={args.turn_timeout}s",
        flush=True,
    )

    await stop.wait()
    LOGGER.info("停机：停止接单，等待在跑会话清理（进程与临时目录由 adapter 收尾）")
    runtime.stop.set()
    unix_server.close()
    await unix_server.wait_closed()
    socket_path.unlink(missing_ok=True)
    if poller is not None:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
    publisher.cancel()
    try:
        await publisher
    except asyncio.CancelledError:
        pass
    LOGGER.info("停机：close 1000 给全部连接（§5.3 Bridge 正常关闭）")
    await server.stop()
    return 0


async def _keepalive_provider(hub: ServeHub):
    """WssServer keepalive_loop 的 provider（必须为协程函数）。"""
    return hub.keepalive()


def run_serve(args: argparse.Namespace) -> int:
    """serve 入口：asyncio 服务跑完后，等 worker 完成在跑会话的清理
    （AppServerClient.stop + 临时目录删除在其 finally 中，红线：真实会话
    必须清理进程与临时目录——绝不留孤儿 codex 进程）。"""
    holder = {}
    try:
        code = asyncio.run(amain(args, holder))
    except KeyboardInterrupt:
        code = 0
    worker = holder.get("worker")
    if worker is not None and worker.is_alive():
        print("bridge_serve_codex: 等待在跑会话收尾（≤%ds）…"
              % (args.turn_timeout * 2 + 60), file=sys.stderr)
        worker.join(timeout=args.turn_timeout * 2 + 60)
    runtime = holder.get("runtime")
    if worker is not None and worker.is_alive():
        print("bridge_serve_codex: ERROR worker 未在超时内结束（可能残留会话进程）",
              file=sys.stderr)
        return 1
    if runtime is not None and runtime.dropped_frames:
        print("bridge_serve_codex: ERROR 发布桥丢帧 %d（Bridge 缺陷）"
              % runtime.dropped_frames, file=sys.stderr)
        return 1
    return code


# ---------------------------------------------------------------------------
# submit：unix socket 投递客户端
# ---------------------------------------------------------------------------

def run_submit(args: argparse.Namespace) -> int:
    ok, resp = serve_codex.submit_via_socket(args.socket_path, args.prompt,
                                             timeout=args.timeout)
    print(json.dumps(resp, ensure_ascii=False))
    if not ok:
        return 3
    return 0


# ---------------------------------------------------------------------------
# subscribe：轻量订户（验收/调试；收快照流 → JSONL + 进度行）
# ---------------------------------------------------------------------------

def run_subscribe(args: argparse.Namespace) -> int:
    from websockets.asyncio import client as ws_client
    from bridge.transports.wss.link import MAX_DOWNLINK_BYTES

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print(f"bridge_serve_codex: token 文件为空: {args.token_file}",
              file=sys.stderr)
        return 2
    out_fh = Path(args.out).open("w", encoding="utf-8") if args.out else None
    states_seen = []
    count = 0

    async def _run() -> int:
        nonlocal count
        try:
            async with ws_client.connect(
                args.url,
                additional_headers={"Authorization": "Bearer %s" % token},
                max_size=MAX_DOWNLINK_BYTES,
                open_timeout=10,
            ) as conn:
                deadline = (time.monotonic() + args.timeout_s) \
                    if args.timeout_s > 0 else None
                while True:
                    remaining = None
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return 4 if args.wait_state else 0
                    try:
                        message = await asyncio.wait_for(conn.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return 4 if args.wait_state else 0
                    count += 1
                    try:
                        snap = json.loads(message)
                    except ValueError:
                        snap = None
                    if out_fh is not None:
                        out_fh.write(message if isinstance(message, str)
                                     else message.decode("utf-8", "replace"))
                        if not message.endswith("\n"):
                            out_fh.write("\n")
                        out_fh.flush()
                    if isinstance(snap, dict):
                        states = [t.get("state") for t in snap.get("threads", [])]
                        states_seen.extend(s for s in states if s)
                        print(
                            "#%d seq=%s epoch=%s states=%s usage=%s"
                            % (count, snap.get("seq"), snap.get("bridge_epoch"),
                               states, (snap.get("usage") or {}).get("available")),
                            file=sys.stderr,
                        )
                        if args.wait_state and args.wait_state in states:
                            return 0
                    if args.max_messages and count >= args.max_messages:
                        return 0
        except Exception as exc:  # noqa: BLE001 — 连接失败如实报告
            print("subscribe error: %s: %s" % (type(exc).__name__, exc),
                  file=sys.stderr)
            return 5
        finally:
            if out_fh is not None:
                out_fh.close()

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bridge_serve_codex",
        description="CodexDT serve-codex: persistent WSS service over the real "
                    "codex app-server adapter (R2; bridge-owned sessions)")
    sub = p.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("serve", help="启动常驻服务")
    ps.add_argument("--host", default="127.0.0.1",
                    help="监听地址（LAN/wss=显式 LAN IP；明文仅限 loopback）")
    ps.add_argument("--port", type=int, default=8765,
                    help="监听端口（默认 8765；与其它服务并存时显式换端口）")
    ps.add_argument("--cert", default=None, help="TLS 证书（LAN wss 必配）")
    ps.add_argument("--key", default=None, help="TLS 私钥（与 --cert 成对）")
    ps.add_argument("--allow-insecure-loopback", action="store_true",
                    help="开发 loopback 明文 ws://（仅 127.0.0.1/::1/localhost；§5.1）")
    ps.add_argument("--token-file", default="config/local/device_token")
    ps.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH,
                    help="submit unix socket 路径")
    ps.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR,
                    help="目录投递目录（*.prompt；空串禁用）")
    ps.add_argument("--evidence-dir", default=DEFAULT_EVIDENCE_DIR)
    ps.add_argument("--codex-bin", default="codex")
    ps.add_argument("--model", default=None,
                    help="固定模型（默认 gpt-5.6-sol；0.152.0 拒账户默认模型）")
    ps.add_argument("--turn-timeout", type=float, default=120.0)
    ps.add_argument("--keepalive-interval-s", type=float, default=15.0)
    ps.add_argument("--poll-interval-s", type=float, default=1.0)
    ps.add_argument("--queue-max", type=int, default=DEFAULT_QUEUE_MAX)
    ps.add_argument("--max-clients", type=int, default=1)
    ps.add_argument("--epoch", default=None, help="bridge_epoch base（默认 codex-serve-<wallms>）")
    ps.add_argument("--verbose", action="store_true")

    pq = sub.add_parser("submit", help="向常驻服务提交 prompt（unix socket）")
    pq.add_argument("--prompt", required=True)
    pq.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    pq.add_argument("--timeout", type=float, default=10.0)

    pb = sub.add_parser("subscribe", help="轻量订户：收快照流（验收用）")
    pb.add_argument("--url", default="ws://127.0.0.1:8765/v1/state")
    pb.add_argument("--token-file", default="config/local/device_token")
    pb.add_argument("--out", default=None, help="快照 JSONL 落盘路径")
    pb.add_argument("--max-messages", type=int, default=0, help="收到 N 份后退出（0=不限）")
    pb.add_argument("--wait-state", default=None,
                    help="任一线程达到该 state（working/needs_you/done…）即退出")
    pb.add_argument("--timeout-s", type=float, default=0, help="整体超时秒（0=不限）")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # 子命令必填；子参数已合并进同一 namespace
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "submit":
        return run_submit(args)
    if args.command == "subscribe":
        return run_subscribe(args)
    return run_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
