"""链路层公共语义（A4-W，P3.3 loopback）。

只承载 protocol/transport.md §5.3/§5.4 冻结的错误分类、退避参数与凭证指纹规则；
不引入任何业务 JSON 语义（红线：Transport 只搬字节）。

- 可重试退避序列（冻结）：1 / 2 / 4 / 8 / 16 / 30 秒，封顶 30s，叠加 ±20% 均匀抖动。
- 连接保持 ≥60s 视为稳定，退避重置回 1s 档。
- CONFIG_ERROR 终态（401/403/TLS 证书失败/1008）：不进入退避循环，只有配置变更
  或人工介入（reset）后才重试。
- 设备 token 永不入日志：对外只允许出现 SHA-256 指纹前 8 hex。
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Tuple

from websockets.exceptions import ConnectionClosed, InvalidStatus

# ---------------------------------------------------------------------------
# 冻结常量（protocol/transport.md §1/§5.3/§5.4；INTERFACES §7 尺寸上限复述）
# ---------------------------------------------------------------------------

#: 下行聚合上限：完整 AppState JSON ≤16384 字节（Bridge → 设备）。
MAX_DOWNLINK_BYTES = 16384
#: 上行聚合上限：DeviceTelemetry JSON ≤512 字节（设备 → Bridge）。
MAX_UPLINK_BYTES = 512

#: 可重试失败的退避档位（秒），封顶 30s。
BACKOFF_SEQUENCE_S: Tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
#: 均匀抖动比例 ±20%。
BACKOFF_JITTER_FRACTION = 0.2
#: 连接保持 ≥60s 稳定 → 重置退避回 1s 档。
BACKOFF_STABLE_RESET_S = 60.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    """开发期 ws:// 明文仅允许 loopback 地址（transport.md §5.1）。"""
    return host in _LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# 配置错误类型
# ---------------------------------------------------------------------------


class TransportConfigError(ValueError):
    """传输配置违宪（如生产关闭 TLS、缺少 pinning）——拒绝启动，不进入运行时。"""


class TlsVerificationError(TransportConfigError):
    """证书/SPKI 指纹校验失败 → 设备侧 CONFIG_ERROR 终态（transport.md §5.3）。"""


# ---------------------------------------------------------------------------
# 失败分类（只依据 HTTP 状态、close code、TLS 异常类型，不含业务语义）
# ---------------------------------------------------------------------------


class FailureKind(Enum):
    """链路失败分类；四个 CONFIG_ERROR 类为终态，RETRYABLE 进入退避。"""

    AUTH_REJECTED = "auth_rejected_401"
    POLICY_REJECTED = "policy_rejected_403"
    CERT_MISMATCH = "cert_or_spki_mismatch"
    AUTH_STATE_LOST = "auth_state_lost_1008"
    RETRYABLE = "retryable_backoff"


CONFIG_ERROR_KINDS = frozenset(
    {
        FailureKind.AUTH_REJECTED,
        FailureKind.POLICY_REJECTED,
        FailureKind.CERT_MISMATCH,
        FailureKind.AUTH_STATE_LOST,
    }
)


def is_config_error(kind: FailureKind) -> bool:
    """CONFIG_ERROR 终态：不重试，仅配置变更/人工介入后再试（§5.4）。"""
    return kind in CONFIG_ERROR_KINDS


def classify_handshake_status(status_code: int) -> FailureKind:
    """HTTP upgrade 应答分类（§5.3 表：401/403 终态；500/503 及其余可重试）。"""
    if status_code == 401:
        return FailureKind.AUTH_REJECTED
    if status_code == 403:
        return FailureKind.POLICY_REJECTED
    return FailureKind.RETRYABLE


def connection_close_code(exc: "ConnectionClosed") -> Optional[int]:
    """从 ConnectionClosed 提取 close code（兼容无 close frame 的异常断链）。

    websockets 17.x 中 `.code` 属性已弃用；本助手用 rcvd/sent 显式取值，
    两者皆无（TCP 层直接断开）→ None，按 1006 语义分类。
    """
    if exc.rcvd is not None and exc.rcvd.code is not None:
        return int(exc.rcvd.code)
    if exc.sent is not None and exc.sent.code is not None:
        return int(exc.sent.code)
    return None


def classify_close_code(close_code: Optional[int]) -> FailureKind:
    """WebSocket close code 分类（§5.3 表）。

    1008 → CONFIG_ERROR 终态；1000/1006/1009/1011 及未列明 code → 可重试退避。
    close_code 为 None 表示无 close frame 的异常断链，按 1006 语义处理。
    """
    if close_code == 1008:
        return FailureKind.AUTH_STATE_LOST
    return FailureKind.RETRYABLE


def classify_exception(exc: BaseException) -> FailureKind:
    """异常分类：SPKI/证书校验失败与 TLS 证书验证失败为终态，其余网络类可重试。"""
    if isinstance(exc, TlsVerificationError):
        return FailureKind.CERT_MISMATCH
    if isinstance(exc, ssl.SSLCertVerificationError):
        # 证书链/主机名验证失败（CA 不匹配、过期、主机名不符）
        return FailureKind.CERT_MISMATCH
    if isinstance(exc, InvalidStatus):
        return classify_handshake_status(exc.response.status_code)
    # OSError / InvalidMessage / TimeoutError / ConnectionError 等 → 网络失联（1006 类）
    return FailureKind.RETRYABLE


# ---------------------------------------------------------------------------
# token 指纹（凭证不入日志：只输出 SHA-256 前 8 hex）
# ---------------------------------------------------------------------------


def token_fingerprint(token: str) -> str:
    """设备 token 的日志安全指纹（SHA-256 前 8 hex）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def bearer_bytes(token: str) -> bytes:
    """构造 Authorization 头字节（Bearer <token>），供常量时间比较。"""
    return f"Bearer {token}".encode("utf-8")


def constant_time_eq(a: bytes, b: bytes) -> bool:
    """常量时间字节比较（认证头校验用）。"""
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# 退避策略（冻结序列 1/2/4/8/16/30s ±20%，≥60s 稳定重置）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffConfig:
    """退避参数。默认值即 transport.md §5.4 冻结值；测试可注入缩小档位。

    缩小档位只改变等待时长、不改变序列形状（1/2/4/... 的倍率关系与
    ±20% 抖动、稳定重置逻辑不变），冻结序列由默认值与单测共同锁定。
    """

    sequence_s: Tuple[float, ...] = BACKOFF_SEQUENCE_S
    jitter_fraction: float = BACKOFF_JITTER_FRACTION
    stable_reset_s: float = BACKOFF_STABLE_RESET_S

    def __post_init__(self) -> None:
        seq = tuple(self.sequence_s)
        if not seq or any(v <= 0 for v in seq):
            raise TransportConfigError("backoff sequence must be non-empty and positive")
        if any(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)):
            raise TransportConfigError("backoff sequence must be strictly increasing")
        if not 0.0 <= self.jitter_fraction <= 1.0:
            raise TransportConfigError("jitter_fraction must be within [0, 1]")
        if self.stable_reset_s <= 0:
            raise TransportConfigError("stable_reset_s must be positive")
        # 冻结校验：用 object.__setattr__ 绕过 frozen 只是读回，不允许改动
        object.__setattr__(self, "sequence_s", seq)


class BackoffPolicy:
    """退避状态机：连续失败推进档位，稳定连接后重置。

    与时钟解耦：调用方（客户端 run 循环或纯逻辑测试）只按需取
    next_delay() 并在断链时报告连接保持时长 record_stable()。
    """

    def __init__(self, config: Optional[BackoffConfig] = None, rng=None) -> None:
        self._cfg = config or BackoffConfig()
        import random

        self._rng = rng if rng is not None else random.Random()
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """自上次重置以来的连续失败次数。"""
        return self._attempts

    @property
    def config(self) -> BackoffConfig:
        return self._cfg

    def next_delay(self) -> float:
        """下一档退避时长（秒），含 ±jitter_fraction 均匀抖动。"""
        seq = self._cfg.sequence_s
        base = seq[min(self._attempts, len(seq) - 1)]
        self._attempts += 1
        jitter = 1.0 + self._rng.uniform(-self._cfg.jitter_fraction, self._cfg.jitter_fraction)
        return base * jitter

    def record_stable(self, held_s: float) -> bool:
        """报告一条连接的实际保持时长；≥stable_reset_s 视为稳定并重置。

        返回是否发生了重置。
        """
        if held_s >= self._cfg.stable_reset_s:
            self._attempts = 0
            return True
        return False

    def reset(self) -> None:
        """人工重置（配置变更/人工介入后回到 1s 档）。"""
        self._attempts = 0


# ---------------------------------------------------------------------------
# 配置文件共用小工具
# ---------------------------------------------------------------------------


def resolve_token(auth_obj: dict, base_dir) -> str:
    """从配置 auth 节解析设备 token（内联或文件），绝不记录内容。

    支持：{"device_token": "..."} 或 {"device_token_file": "路径"}
    （相对路径相对配置文件所在目录解析）。两者皆缺 → TransportConfigError。
    """
    import pathlib

    base = pathlib.Path(base_dir)
    if isinstance(auth_obj, dict) and auth_obj.get("device_token_file"):
        p = pathlib.Path(str(auth_obj["device_token_file"]))
        if not p.is_absolute():
            p = base / p
        token = p.read_text(encoding="utf-8").strip()
    elif isinstance(auth_obj, dict) and isinstance(auth_obj.get("device_token"), str):
        token = auth_obj["device_token"].strip()
    else:
        raise TransportConfigError("auth 节需要 device_token 或 device_token_file")
    if not token:
        raise TransportConfigError("device token 不能为空")
    return token


def warn_on_unknown_keys(obj: dict, known: Iterable[str], where: str, logger=None) -> None:
    """未知配置键前向兼容：忽略并记 warning（不记录值内容）。"""
    unknown = sorted(set(obj) - set(known) - {"_comment"})
    if unknown and logger is not None:
        logger.warning("%s: 忽略未知配置键 %s", where, unknown)
