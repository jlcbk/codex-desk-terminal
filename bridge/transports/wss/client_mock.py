"""Python mock 设备客户端（A4-W，P3.3 loopback）。

模拟 ESP32 WSS 客户端行为（protocol/transport.md §5 设备侧冻结语义），
用于 loopback 语义验证；不含任何业务 JSON 解析（红线：只搬字节）。

- upgrade 携带 Authorization: Bearer <device-token>（配置注入，日志只记指纹）。
- 失败分类（§5.3 表）：
  * 401 / 403 / TLS 证书或 SPKI 指纹失败 / close 1008 → CONFIG_ERROR 终态，
    不重试（stats.retry 计数保持），仅 reset()（配置变更/人工介入）后再试；
  * HTTP 500/503、close 1000/1006/1009/1011、网络失联 → 退避重连
    1/2/4/8/16/30s ±20% 抖动，连接保持 ≥60s 稳定重置回 1s 档（§5.4）。
- 升级成功后首条 text message 即完整 AppState 快照，原样交 on_message；
  收到聚合 >16384 字节下行：计数并按 1009 处理（关闭重连），不解析不应用。
- 上行遥测 ≤512 字节独立上限（send_telemetry → accepted/busy/error）。
- WebSocket ping/pong 心跳沿用 websockets 默认 20s/20s（§5.2 步 5）。

依赖锁：websockets==17.1（docs/VERSIONS.md）；运行于 Python 3.12（uv）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from .link import (
    MAX_DOWNLINK_BYTES,
    MAX_UPLINK_BYTES,
    BackoffConfig,
    BackoffPolicy,
    FailureKind,
    TlsVerificationError,
    TransportConfigError,
    bearer_bytes,
    classify_close_code,
    classify_exception,
    connection_close_code,
    is_config_error,
    is_loopback_host,
    resolve_token,
    token_fingerprint,
    warn_on_unknown_keys,
)
from .tlsutil import build_client_ssl_context, peer_cert_der_from_connection, verify_spki_sha256_hex

LOGGER = logging.getLogger("cdt.transport.wss.client")

MessageCallback = Callable[[bytes, int], None]
LinkCallback = Callable[["LinkState", object], None]


class LinkState(Enum):
    """设备侧链路状态（on_link 上报）。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CONFIG_ERROR = "config_error"
    STOPPED = "stopped"


class SendStatus(Enum):
    """上行发送三值结果（INTERFACES §5 send 语义）。"""

    ACCEPTED = "accepted"
    BUSY = "busy"  # 连接建立中
    ERROR = "error"  # 未连接 / 超过 512 字节上限


@dataclass(frozen=True)
class WssClientConfig:
    """mock 设备客户端配置（fail-closed：wss 必须 ca+SPKI pinning）。"""

    url: str
    #: dev 标记：明文 ws:// 仅限 loopback（transport.md §5.1）
    dev_insecure_loopback: bool = False
    #: 自签 CA 证书路径（§5.5 ①，wss 必填）
    ca_file: Optional[str] = None
    #: 叶子证书 SPKI SHA-256 hex（§5.5 ②，wss 必填）
    spki_sha256_hex: Optional[str] = None
    #: 主机名校验仅在 dev 特殊场景显式放宽；证书链验证永不关闭
    check_hostname: bool = True
    connect_timeout_s: float = 10.0
    ping_interval_s: Optional[float] = 20.0
    ping_timeout_s: Optional[float] = 20.0
    backoff: BackoffConfig = field(default_factory=BackoffConfig)

    def validate(self) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self.url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss"):
            raise TransportConfigError(f"url scheme 必须为 ws/wss，got {self.url!r}")
        if scheme == "ws":
            if not self.dev_insecure_loopback:
                raise TransportConfigError(
                    "明文 ws:// 需要显式 dev_insecure_loopback=True（仅开发 loopback）"
                )
            if not is_loopback_host(parsed.hostname or ""):
                raise TransportConfigError(
                    f"明文 ws:// 仅限 loopback 主机，got {parsed.hostname!r}"
                )
        else:  # wss
            if not self.ca_file:
                raise TransportConfigError("wss 必须配置 ca_file（生产禁止关闭证书验证）")
            if not self.spki_sha256_hex:
                raise TransportConfigError("wss 必须配置 spki_sha256_hex（§5.5 ② pinning）")


@dataclass
class ClientStats:
    """设备侧行为统计（模拟真机计数器）。"""

    attempts: int = 0
    connected_count: int = 0
    config_errors: int = 0
    #: CONFIG_ERROR 后的重试计数（冻结要求终态不重试 → 恒为 0）
    retries_after_config_error: int = 0
    retry_delays_s: List[float] = field(default_factory=list)
    close_codes: List[Optional[int]] = field(default_factory=list)
    failure_kinds: List[str] = field(default_factory=list)
    messages_received: int = 0
    bytes_received: int = 0
    oversize_rejected: int = 0
    telemetry_accepted: int = 0
    last_failure: Optional[str] = None


class MockDeviceClient:
    """模拟设备：连接、收快照、按冻结错误码退避重连、统计。"""

    def __init__(
        self,
        config: WssClientConfig,
        *,
        device_token: str,
        on_message: Optional[MessageCallback] = None,
        on_link: Optional[LinkCallback] = None,
        rng=None,
        logger: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        config.validate()
        if not device_token:
            raise TransportConfigError("device_token 不能为空")
        self._cfg = config
        self._bearer = bearer_bytes(device_token)
        self._token_fp = token_fingerprint(device_token)
        self._on_message = on_message
        self._on_link = on_link
        self._log = logger or LOGGER
        self._clock = clock
        self._sleep = sleep
        self._backoff = BackoffPolicy(config.backoff, rng=rng)
        self.stats = ClientStats()
        self._link = LinkState.DISCONNECTED
        self._conn: Optional[ClientConnection] = None
        self._stop_requested = False
        self._terminal = False  # CONFIG_ERROR 终态

    # -- 状态与观测 ---------------------------------------------------------

    @property
    def config(self) -> WssClientConfig:
        return self._cfg

    @property
    def link_state(self) -> LinkState:
        return self._link

    def _set_link(self, state: LinkState, detail: object = None) -> None:
        self._link = state
        self._log.debug("link -> %s (detail=%r)", state.value, detail)
        if self._on_link is not None:
            self._on_link(state, detail)

    def reset(self) -> None:
        """配置变更/人工介入后解除 CONFIG_ERROR 终态（§5.4 唯一重试入口）。"""
        self._terminal = False
        self._backoff.reset()
        if self._link is LinkState.CONFIG_ERROR:
            self._set_link(LinkState.DISCONNECTED)

    def request_stop(self) -> None:
        """同步停机请求（信号处理用）；实际关闭在事件循环内完成。"""
        self._stop_requested = True
        conn = self._conn
        if conn is not None:
            asyncio.ensure_future(self.stop())

    async def stop(self) -> None:
        """停机：置位并以 close 1000 主动断开，run() 循环随之退出。"""
        self._stop_requested = True
        conn = self._conn
        if conn is not None:
            try:
                await conn.close(code=1000, reason="device stop")
            except Exception:  # noqa: BLE001
                pass

    # -- 主循环 -------------------------------------------------------------

    async def run(self) -> LinkState:
        """连接/退避主循环；返回最终 LinkState。

        CONFIG_ERROR 终态后直接返回（不重试）；再次运行需先 reset()。
        """
        if self._terminal:
            return self._link
        ssl_ctx = None
        if self._cfg.url.lower().startswith("wss"):
            ssl_ctx = build_client_ssl_context(
                ca_file=self._cfg.ca_file or "",
                check_hostname=self._cfg.check_hostname,
            )
        while not self._stop_requested:
            self._set_link(LinkState.CONNECTING)
            self.stats.attempts += 1
            attempt = self._attempt(ssl_ctx)
            held_at = self._clock()
            try:
                await attempt
                held = self._clock() - held_at
            except BaseException as exc:  # noqa: BLE001 — 统一分类，原样不吞 Cancelled
                if isinstance(exc, asyncio.CancelledError):
                    raise
                held = self._clock() - held_at
                if not await self._on_failure(exc, held):
                    return self._link
                continue
            # 正常返回（stop 或远端关闭已在 _attempt 内计数）
            self._backoff.record_stable(held)
        self._set_link(LinkState.STOPPED)
        return self._link

    async def _attempt(self, ssl_ctx) -> None:
        try:
            async with connect(
                self._cfg.url,
                additional_headers={"Authorization": self._bearer.decode("ascii")},
                ssl=ssl_ctx,
                open_timeout=self._cfg.connect_timeout_s,
                max_size=MAX_DOWNLINK_BYTES,  # 下行聚合上限：超限库侧 close 1009
                ping_interval=self._cfg.ping_interval_s,
                ping_timeout=self._cfg.ping_timeout_s,
            ) as conn:
                self._apply_spki_pinning(conn)
                self._conn = conn
                self.stats.connected_count += 1
                self._set_link(LinkState.CONNECTED)
                await self._recv_loop(conn)
        finally:
            self._conn = None

    def _apply_spki_pinning(self, conn: ClientConnection) -> None:
        """叶子证书 SPKI SHA-256 pinning（§5.5 ②；失败 → CONFIG_ERROR 终态）。"""
        expected = self._cfg.spki_sha256_hex
        if not expected:
            return
        der = peer_cert_der_from_connection(conn)
        if der is None:
            raise TlsVerificationError("wss 连接缺少 TLS 层，无法执行 SPKI pinning")
        verify_spki_sha256_hex(der, expected)

    async def _recv_loop(self, conn: ClientConnection) -> None:
        while True:
            message = await conn.recv()  # ConnectionClosed 结束
            data = message.encode("utf-8") if isinstance(message, str) else bytes(message)
            if len(data) > MAX_DOWNLINK_BYTES:
                # 防御性复查（max_size 已兜底）：close 1009 + 退避重连（§5.3）
                self._log.error("下行 %d 字节超上限，close 1009", len(data))
                await conn.close(code=1009, reason="downlink oversize")
                # 计数在 _on_failure 按 close code 1009 统一进行（含库 max_size 路径）
                raise ConnectionClosed(None, Close(1009, "downlink oversize"))
            self.stats.messages_received += 1
            self.stats.bytes_received += len(data)
            if self._on_message is not None:
                self._on_message(data, len(data))

    async def _on_failure(self, exc: BaseException, held_s: float) -> bool:
        """统一失败处理；返回 True 表示将继续退避重试，False 表示循环结束。"""
        if isinstance(exc, ConnectionClosed):
            code = connection_close_code(exc)
            kind = classify_close_code(code)
            if code == 1009:
                # 聚合超上限（库 max_size 兜底或防御路径，均为下行方向）：计数（§5.3）
                self.stats.oversize_rejected += 1
        else:
            kind = classify_exception(exc)
            code = None
        self.stats.close_codes.append(code)
        self.stats.failure_kinds.append(kind.value)
        self.stats.last_failure = kind.value
        self._log.info(
            "连接失败 kind=%s code=%s held=%.1fs token fingerprint=%s",
            kind.value,
            code if code is not None else "none(1006)",
            held_s,
            self._token_fp,
        )
        if self._stop_requested:
            self._set_link(LinkState.DISCONNECTED, code)
            return False
        if is_config_error(kind):
            # 终态：不重试（§5.4），重试计数保持 0
            self.stats.config_errors += 1
            self._terminal = True
            self._set_link(LinkState.CONFIG_ERROR, kind.value)
            return False
        # 可重试：≥60s 稳定连接先重置退避，再取下一档
        if self._backoff.record_stable(held_s):
            self._log.info("连接保持 %.1fs 稳定，退避已重置回 1s 档", held_s)
        delay = self._backoff.next_delay()
        self.stats.retry_delays_s.append(delay)
        self._set_link(LinkState.DISCONNECTED, code)
        await self._sleep(delay)
        return True

    # -- 上行遥测（独立 ≤512 字节上限） --------------------------------------

    async def send_telemetry(self, data: bytes) -> SendStatus:
        """发送 DeviceTelemetry 字节（独立上行上限 512，独立计数）。

        Transport 不读取内容；仅按尺寸与链路状态给出三值结果。
        """
        conn = self._conn
        if self._link is not LinkState.CONNECTED or conn is None:
            return SendStatus.BUSY if self._link is LinkState.CONNECTING else SendStatus.ERROR
        if len(data) > MAX_UPLINK_BYTES:
            self._log.error("上行 %d 字节超过独立上限 %d，拒绝发送", len(data), MAX_UPLINK_BYTES)
            return SendStatus.ERROR
        # 冻结帧格式：上行遥测同样为 WebSocket text message（§5.1）
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._log.error("遥测不是合法 UTF-8，拒绝发送（Transport 不修补内容）")
            return SendStatus.ERROR
        try:
            await conn.send(text)
        except ConnectionClosed:
            self._conn = None
            return SendStatus.ERROR
        self.stats.telemetry_accepted += 1
        return SendStatus.ACCEPTED


# ---------------------------------------------------------------------------
# 配置文件加载与 CLI
# ---------------------------------------------------------------------------

_CLIENT_KNOWN_KEYS = {"url", "tls", "auth", "check_hostname"}


def load_client_config(config_path: str) -> "tuple[WssClientConfig, str]":
    """加载客户端配置 JSON，返回 (config, device_token)。token 不进日志。"""
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    warn_on_unknown_keys(raw, _CLIENT_KNOWN_KEYS, f"client config {path.name}", LOGGER)
    tls = raw.get("tls") or {}
    base = path.resolve().parent
    token = resolve_token(raw.get("auth") or {}, base)
    cfg = WssClientConfig(
        url=str(raw["url"]),
        ca_file=tls.get("ca_file"),
        spki_sha256_hex=tls.get("spki_sha256_hex"),
        check_hostname=bool(raw.get("check_hostname", True)),
    )
    cfg.validate()
    return cfg, token


def _main(argv: Optional[List[str]] = None) -> int:
    """开发运行入口：连上后收快照并打印计数，可选发送演示遥测。

    示例：
      uv run --python 3.12 --with 'websockets==17.1' python -m \
        bridge.transports.wss.client_mock --config config/local/wss_client_mock.json \
        --max-messages 3
    """
    import argparse

    parser = argparse.ArgumentParser(description="CodexDT mock WSS device client (P3.3 dev)")
    parser.add_argument("--config", required=True, help="客户端配置 JSON（config/local/）")
    parser.add_argument("--max-messages", type=int, default=0,
                        help="收到 N 条后退出（0=直到 Ctrl+C）")
    parser.add_argument("--telemetry-every", type=int, default=0,
                        help="每收到 N 条发送一次演示遥测（0=不发送）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg, token = load_client_config(args.config)
    received = {"count": 0}

    def on_message(data: bytes, n: int) -> None:
        received["count"] += 1
        LOGGER.info("快照 #%d 收到 %d 字节（fingerprint of bytes=%s）",
                    received["count"], n, token_fingerprint(data.decode("utf-8", "replace")))

    async def telemetry_tick(client: MockDeviceClient) -> None:
        payload = json.dumps(
            {"schema_version": 1, "kind": "telemetry", "battery_mv": None,
             "battery_valid": False, "rssi_dbm": None, "transport": "wifi",
             "sent_at_ms": None},
            separators=(",", ":"),
        ).encode("utf-8")
        status = await client.send_telemetry(payload)
        LOGGER.info("演示遥测发送: %s", status.value)

    async def amain() -> int:
        client = MockDeviceClient(cfg, device_token=token, on_message=on_message)
        task = asyncio.create_task(client.run())
        last_seen = 0
        while True:
            await asyncio.sleep(0.2)
            state = client.link_state
            count = client.stats.messages_received
            if count != last_seen and count > 0:
                last_seen = count
                if args.telemetry_every and count % args.telemetry_every == 0:
                    await telemetry_tick(client)
            if state is LinkState.CONFIG_ERROR:
                task.cancel()
                break
            if args.max_messages and count >= args.max_messages:
                await client.stop()
                break
            if task.done():
                break
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        print(json.dumps({
            "link_state": client.link_state.value,
            "attempts": client.stats.attempts,
            "connected_count": client.stats.connected_count,
            "config_errors": client.stats.config_errors,
            "retries_after_config_error": client.stats.retries_after_config_error,
            "retry_delays_s": [round(d, 3) for d in client.stats.retry_delays_s],
            "close_codes": client.stats.close_codes,
            "messages_received": client.stats.messages_received,
            "bytes_received": client.stats.bytes_received,
            "oversize_rejected": client.stats.oversize_rejected,
            "telemetry_accepted": client.stats.telemetry_accepted,
        }, ensure_ascii=False))
        return 0 if client.link_state in (LinkState.STOPPED, LinkState.CONNECTED) else 1

    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(_main())
