"""Bridge WSS server（A4-W，P3.3 loopback；protocol/transport.md §5 冻结语义）。

角色：ESP32 为 WSS 客户端，Bridge 为服务端（§5.1）。

- 业务路径冻结 /v1/state；其余路径 HTTP 404 并关闭。
- 认证：Authorization: Bearer <device-token>（配置注入，不入 Git/日志；
  日志只记 token 指纹前 8 hex）。缺失/错误 → HTTP 401；策略拒绝（如已连
  设备数超上限）→ HTTP 403。两者对设备均为 CONFIG_ERROR 终态（§5.3）。
- TLS：生产必须 wss（设备侧自签 CA 验链 + SPKI SHA-256 pinning，§5.5）；
  开发 loopback 允许 ws:// 仅限 127.0.0.1/::1/localhost 且必须显式
  allow_insecure_loopback=True（§5.1），否则拒绝启动。
- 连接建立后立即发送完整快照（§5.2 步 3）；此后每次 send()/publish 推送
  最新快照；可配置存活快照间隔（默认 15s，经 snapshot_provider 取最新字节，
  字节未变化不重复发送）。seq/epoch 推进是业务层（StateEngine）职责。
- 红线：只搬字节。快照由业务层以 bytes 供给，服务端唯一校验 = 聚合
  ≤16384 字节；超限视为 Bridge 缺陷（§5.3：Bridge 侧禁止发生），拒绝发送
  并计数，绝不解析、不修补。
- 上行遥测：独立 ≤512 字节上限，max_size 由 websockets 强制（超限 close
  1009）；收到的遥测字节原样经 on_message 回调交付，不读取内容。
- 停机：向所有连接发送 close 1000（§5.3 Bridge 正常关闭）。

依赖锁：websockets==17.1（docs/VERSIONS.md）；运行于 Python 3.12（uv）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .link import (
    MAX_DOWNLINK_BYTES,
    MAX_UPLINK_BYTES,
    TransportConfigError,
    bearer_bytes,
    constant_time_eq,
    is_loopback_host,
    resolve_token,
    token_fingerprint,
    warn_on_unknown_keys,
)
from .tlsutil import build_server_ssl_context

LOGGER = logging.getLogger("cdt.transport.wss.server")

#: 业务路径（transport.md §5.1 冻结；配置必须与此一致）。
FROZEN_PATH = "/v1/state"

#: 存活快照默认间隔（INTERFACES §4：默认每 15 秒一份全量存活快照）。
DEFAULT_KEEPALIVE_INTERVAL_S = 15.0

SnapshotProvider = Callable[[], Awaitable[Optional[bytes]]]
#: on_link(status, detail)：("connected", peer) / ("disconnected", close_code|None)
LinkCallback = Callable[[str, object], None]
#: on_message(data, len)：上行遥测字节透传（Transport 不读取内容）。
MessageCallback = Callable[[bytes, int], None]


class SendStatus(Enum):
    """INTERFACES §5 Transport.send 三值结果。"""

    ACCEPTED = "accepted"
    BUSY = "busy"
    ERROR = "error"


@dataclass(frozen=True)
class WssServerConfig:
    """服务端配置。TLS 与 token 均经本地文件/配置注入，不入 Git。"""

    host: str = "127.0.0.1"
    port: int = 8765
    path: str = FROZEN_PATH
    max_clients: int = 1
    keepalive_interval_s: Optional[float] = DEFAULT_KEEPALIVE_INTERVAL_S
    ping_interval_s: Optional[float] = 20.0
    ping_timeout_s: Optional[float] = 20.0
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    #: 开发 loopback 明文 ws:// 的显式 dev 标记（§5.1）；生产禁止置 True 之外的
    #: 组合——本标记为 True 时 host 还必须落在本机 loopback 集合内。
    allow_insecure_loopback: bool = False

    def validate(self) -> None:
        if self.path != FROZEN_PATH:
            raise TransportConfigError(f"业务路径冻结为 {FROZEN_PATH}（transport.md §5.1），got {self.path!r}")
        if self.max_clients < 1:
            raise TransportConfigError("max_clients 必须 ≥1")
        if self.keepalive_interval_s is not None and self.keepalive_interval_s <= 0:
            raise TransportConfigError("keepalive_interval_s 必须 >0 或 None（禁用）")
        has_cert = self.tls_cert_file is not None
        has_key = self.tls_key_file is not None
        if has_cert != has_key:
            raise TransportConfigError("TLS 需要 tls_cert_file 与 tls_key_file 成对配置")
        if not has_cert:
            if not self.allow_insecure_loopback:
                raise TransportConfigError(
                    "生产配置禁止关闭 TLS（transport.md §5.1/§5.5）；开发 loopback 明文 ws:// "
                    "必须显式 allow_insecure_loopback=True"
                )
            if not is_loopback_host(self.host):
                raise TransportConfigError(
                    f"明文 ws:// 仅限 loopback 地址 {sorted(('127.0.0.1', '::1', 'localhost'))}，got {self.host!r}"
                )


@dataclass
class WssServerStats:
    """运行统计（诊断用；不含 payload 内容）。"""

    connections_accepted: int = 0
    rejected_path_404: int = 0
    rejected_auth_401: int = 0
    rejected_policy_403: int = 0
    snapshots_pushed: int = 0
    snapshots_deduped: int = 0
    oversize_out_rejected: int = 0  # Bridge 缺陷计数：业务层给出 >16384 字节快照
    non_utf8_out_rejected: int = 0  # Bridge 缺陷计数：快照不是合法 UTF-8（text frame 要求）
    uplink_messages: int = 0
    uplink_bytes: int = 0
    uplink_binary_ignored: int = 0


class WssServer:
    """WSS 服务端：认证、全量快照推送、上行遥测透传（只搬字节）。"""

    def __init__(
        self,
        config: WssServerConfig,
        *,
        device_token: str,
        snapshot_provider: Optional[SnapshotProvider] = None,
        on_message: Optional[MessageCallback] = None,
        on_link: Optional[LinkCallback] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        config.validate()
        if not device_token:
            raise TransportConfigError("device_token 不能为空")
        self._cfg = config
        self._bearer = bearer_bytes(device_token)
        self._token_fp = token_fingerprint(device_token)
        self._provider = snapshot_provider
        self._on_message = on_message
        self._on_link = on_link
        self._log = logger or LOGGER
        self._server = None
        self._keepalive_task: Optional[asyncio.Task] = None
        # 认证通过的连接 → 该连接最后收到的快照字节（去重推送）
        self._conns: Dict[ServerConnection, Optional[bytes]] = {}
        self._current: Optional[bytes] = None
        self.stats = WssServerStats()

    # -- 生命周期（INTERFACES §5：start/stop） ------------------------------

    @property
    def config(self) -> WssServerConfig:
        return self._cfg

    @property
    def bound_port(self) -> Optional[int]:
        """start() 后实际绑定端口（port=0 时取临时端口；测试用）。"""
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    @property
    def client_count(self) -> int:
        return len(self._conns)

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("WssServer 已启动")
        ssl_ctx = None
        if self._cfg.tls_cert_file is not None:
            ssl_ctx = build_server_ssl_context(self._cfg.tls_cert_file, self._cfg.tls_key_file)
        scheme = "wss" if ssl_ctx is not None else "ws(dev-loopback)"
        self._server = await serve(
            self._handler,
            self._cfg.host,
            self._cfg.port,
            ssl=ssl_ctx,
            process_request=self._process_request,
            max_size=MAX_UPLINK_BYTES,  # 上行聚合上限：超限由库以 close 1009 拒绝
            ping_interval=self._cfg.ping_interval_s,
            ping_timeout=self._cfg.ping_timeout_s,
        )
        if self._cfg.keepalive_interval_s is not None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="cdt-wss-keepalive"
            )
        self._log.info(
            "wss server listening on %s://%s:%d%s (token fingerprint=%s, max_clients=%d)",
            scheme,
            self._cfg.host,
            self.bound_port or self._cfg.port,
            self._cfg.path,
            self._token_fp,
            self._cfg.max_clients,
        )

    async def stop(self) -> None:
        """停机：向所有连接发送 close 1000（§5.3 Bridge 正常关闭）后关闭监听。"""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        for conn in list(self._conns):
            try:
                await conn.close(code=1000, reason="bridge shutdown")
            except Exception:  # noqa: BLE001 — 停机路径尽力关闭
                pass
        if self._server is not None:
            self._server.close(close_connections=False)
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        self._conns.clear()

    # -- 快照下发（INTERFACES §5：send(bytes,len) → accepted/busy/error） —

    async def send(self, data: bytes) -> SendStatus:
        """接收业务层给出的最新完整快照字节并推送（不解析、不修补）。

        - 聚合 >16384 字节 = Bridge 缺陷：拒绝发送并计数（§5.3），返回 ERROR。
        - 未启动返回 BUSY；否则存储为当前快照并推送给尚未收到相同字节的
          认证连接（重连总取得全量，存储即最终送达），返回 ACCEPTED。
        """
        if self._server is None:
            return SendStatus.BUSY
        if not isinstance(data, (bytes, bytearray)):
            self._log.error("send() 只接受 bytes（Transport 不做序列化）")
            return SendStatus.ERROR
        data = bytes(data)
        if len(data) > MAX_DOWNLINK_BYTES:
            self.stats.oversize_out_rejected += 1
            self._log.error(
                "快照聚合 %d 字节超过下行上限 %d（Bridge 缺陷），拒绝发送（计数=%d）",
                len(data),
                MAX_DOWNLINK_BYTES,
                self.stats.oversize_out_rejected,
            )
            return SendStatus.ERROR
        self._current = data
        pushed = await self._broadcast(data)
        return SendStatus.ACCEPTED

    #: 别名：业务层语义化的"发布新快照"。
    publish = send

    async def _broadcast(self, data: bytes) -> int:
        pushed = 0
        for conn in list(self._conns):
            if self._conns.get(conn) == data:
                self.stats.snapshots_deduped += 1
                continue
            if await self._send_to(conn, data):
                pushed += 1
        return pushed

    async def _send_to(self, conn: ServerConnection, data: bytes) -> bool:
        if conn not in self._conns:
            return False
        # 冻结帧格式：每条下行消息为 WebSocket text message（§5.1）。
        # UTF-8 解码仅为线格式要求（text frame），不解释内容；解码失败视为
        # Bridge 缺陷（payload 必须是 UTF-8 JSON），拒绝发送并计数。
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.stats.non_utf8_out_rejected += 1
            self._log.error(
                "快照不是合法 UTF-8（Bridge 缺陷），拒绝发送（计数=%d）",
                self.stats.non_utf8_out_rejected,
            )
            return False
        try:
            await conn.send(text)
        except ConnectionClosed:
            return False
        self._conns[conn] = data
        self.stats.snapshots_pushed += 1
        return True

    async def _keepalive_loop(self) -> None:
        """存活快照：周期性向业务层要最新快照字节；字节变化才推送。

        seq 递增（存活快照也占 seq）由 provider 背后的 StateEngine 负责；
        Transport 只按字节判等去重。
        """
        interval = float(self._cfg.keepalive_interval_s or 0)
        while True:
            await asyncio.sleep(interval)
            if self._provider is None:
                continue
            try:
                data = await self._provider()
            except Exception as exc:  # noqa: BLE001 — provider 属业务层，失败不终止心跳
                self._log.error("snapshot_provider 失败: %s: %s", type(exc).__name__, exc)
                continue
            if data is None:
                continue
            status = await self.send(data)
            if status is SendStatus.ERROR:
                # 缺陷已计数；继续心跳，等待业务层给出合法尺寸快照
                continue

    # -- 握手（§5.2） -------------------------------------------------------

    async def _process_request(
        self, conn: ServerConnection, request: Request
    ) -> Optional[Response]:
        if request.path != self._cfg.path:
            self.stats.rejected_path_404 += 1
            self._log.info("拒绝路径 %r（冻结路径 %s）", request.path, self._cfg.path)
            return Response(404, "Not Found", Headers())
        auth = request.headers.get("Authorization") or ""
        if not constant_time_eq(auth.encode("utf-8", errors="replace"), self._bearer):
            self.stats.rejected_auth_401 += 1
            self._log.info(
                "认证失败（缺失或 token 错误）；对端=%s 提供指纹=%s（本端 fingerprint=%s）",
                conn.remote_address,
                token_fingerprint(auth.removeprefix("Bearer ")) if auth else "none",
                self._token_fp,
            )
            return Response(401, "Unauthorized", Headers())
        if len(self._conns) >= self._cfg.max_clients:
            self.stats.rejected_policy_403 += 1
            self._log.info(
                "策略拒绝（已连设备数 %d 达上限 %d）", len(self._conns), self._cfg.max_clients
            )
            return Response(403, "Forbidden", Headers())
        # 认证通过即在 process_request 期占位，杜绝并发升级竞态挤占 max_clients；
        # handler finally 与 _reap 双路径清理。
        self._conns[conn] = None
        self.stats.connections_accepted += 1
        asyncio.create_task(self._reap(conn), name="cdt-wss-reap")
        return None

    async def _reap(self, conn: ServerConnection) -> None:
        try:
            await conn.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._conns.pop(conn, None)

    async def _handler(self, conn: ServerConnection) -> None:
        """升级后的连接处理：立即发送完整快照（§5.2 步 3），随后透传上行。"""
        try:
            initial = self._current
            if initial is None and self._provider is not None:
                initial = await self._provider()
                if initial is not None and len(initial) > MAX_DOWNLINK_BYTES:
                    self.stats.oversize_out_rejected += 1
                    self._log.error("首帧快照 %d 字节超上限（Bridge 缺陷），不发送", len(initial))
                    initial = None
            if initial is not None:
                await self._send_to(conn, initial)
            if self._on_link is not None:
                self._on_link("connected", conn.remote_address)
            await self._recv_loop(conn)
        except ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001 — 服务端内部错误 → 1011（设备可重试）
            self._log.error("连接处理内部错误: %s: %s", type(exc).__name__, exc)
            try:
                await conn.close(code=1011, reason="internal error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            was_client = conn in self._conns
            self._conns.pop(conn, None)
            if was_client and self._on_link is not None:
                self._on_link("disconnected", conn.close_code)

    async def _recv_loop(self, conn: ServerConnection) -> None:
        async for message in conn:
            if isinstance(message, str):
                data = message.encode("utf-8")
            else:
                # v1 不使用 binary frame（§5.1）：计数并忽略，不解析
                self.stats.uplink_binary_ignored += 1
                continue
            if len(data) > MAX_UPLINK_BYTES:  # max_size 之外的防御性复查
                self._log.info("上行 %d 字节超上限，close 1009", len(data))
                await conn.close(code=1009, reason="uplink oversize")
                break
            self.stats.uplink_messages += 1
            self.stats.uplink_bytes += len(data)
            if self._on_message is not None:
                self._on_message(data, len(data))


# ---------------------------------------------------------------------------
# 配置文件加载与 CLI（token/私钥只走本地文件，config/local/ 已 gitignore）
# ---------------------------------------------------------------------------

_SERVER_KNOWN_KEYS = {
    "host",
    "port",
    "path",
    "max_clients",
    "keepalive_interval_s",
    "ping_interval_s",
    "ping_timeout_s",
    "tls",
    "auth",
    "allow_insecure_loopback",
}


def load_server_config(config_path: str) -> "tuple[WssServerConfig, str]":
    """加载服务端配置 JSON，返回 (config, device_token)。

    token 支持内联 device_token 或 device_token_file（相对路径相对配置文件
    目录解析）。token 不进入任何日志/异常文本。
    """
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    warn_on_unknown_keys(raw, _SERVER_KNOWN_KEYS, f"server config {path.name}", LOGGER)
    tls = raw.get("tls") or {}
    base = path.resolve().parent
    token = resolve_token(raw.get("auth") or {}, base)
    cfg = WssServerConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8765)),
        path=str(raw.get("path", FROZEN_PATH)),
        max_clients=int(raw.get("max_clients", 1)),
        keepalive_interval_s=(
            None if raw.get("keepalive_interval_s") is None
            else float(raw.get("keepalive_interval_s"))
        ),
        ping_interval_s=(
            None if raw.get("ping_interval_s") is None else float(raw.get("ping_interval_s"))
        ),
        ping_timeout_s=(
            None if raw.get("ping_timeout_s") is None else float(raw.get("ping_timeout_s"))
        ),
        tls_cert_file=tls.get("cert_file"),
        tls_key_file=tls.get("key_file"),
        allow_insecure_loopback=bool(raw.get("allow_insecure_loopback", False)),
    )
    cfg.validate()
    return cfg, token


def _main(argv: Optional[List[str]] = None) -> int:
    """开发运行入口（dev 助手；快照来源为字节文件，业务接线归 StateEngine 集成）。

    示例：
      uv run --python 3.12 --with 'websockets==17.1' python -m \
        bridge.transports.wss.server --config config/local/wss_server.json \
        --snapshot-file tests/fixtures/protocol/valid_minimal.json
    """
    import argparse

    parser = argparse.ArgumentParser(description="CodexDT Bridge WSS server (P3.3 dev)")
    parser.add_argument("--config", required=True, help="服务端配置 JSON（config/local/）")
    parser.add_argument(
        "--snapshot-file",
        help="开发用快照字节来源（原样透传，不解析）；业务集成时改由 StateEngine 推送",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg, token = load_server_config(args.config)
    snapshot_holder: Dict[str, Optional[bytes]] = {"data": None}
    if args.snapshot_file:
        data = Path(args.snapshot_file).read_bytes()
        if len(data) > MAX_DOWNLINK_BYTES:
            parser.error(f"snapshot-file {len(data)} 字节超上限 {MAX_DOWNLINK_BYTES}")
        snapshot_holder["data"] = data

    async def provider() -> Optional[bytes]:
        return snapshot_holder["data"]

    async def amain() -> int:
        server = WssServer(cfg, device_token=token, snapshot_provider=provider)
        await server.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # 非 POSIX
                pass
        await stop.wait()
        await server.stop()
        return 0

    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(_main())
