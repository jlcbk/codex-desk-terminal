"""WSS Transport（A4-W，P3.3 loopback；bridge/transports/wss/）。

角色冻结（protocol/transport.md §5）：ESP32 为 WSS 客户端，Bridge 为服务端；
业务路径 /v1/state；每条 WebSocket text message 一份完整 AppState（聚合
≤16384 字节），上行遥测独立 ≤512 字节。Transport 只搬字节：不生成、不修补、
不解析业务 JSON，校验仅限尺寸上限与链路层错误码/退避语义（§5.3/§5.4）。

依赖锁：websockets==17.1（docs/VERSIONS.md）；运行于 Python 3.12（uv 注入）：

    uv run --python 3.12 --with 'websockets==17.1' python -m bridge.transports.wss.server  --help
    uv run --python 3.12 --with 'websockets==17.1' python -m bridge.transports.wss.client_mock --help
"""

from .client_mock import (
    ClientStats,
    LinkState,
    MockDeviceClient,
    SendStatus,
    WssClientConfig,
    load_client_config,
)
from .link import (
    BACKOFF_JITTER_FRACTION,
    BACKOFF_SEQUENCE_S,
    BACKOFF_STABLE_RESET_S,
    MAX_DOWNLINK_BYTES,
    MAX_UPLINK_BYTES,
    BackoffConfig,
    BackoffPolicy,
    FailureKind,
    TlsVerificationError,
    TransportConfigError,
    classify_close_code,
    classify_exception,
    classify_handshake_status,
    connection_close_code,
    is_config_error,
    token_fingerprint,
)
from .server import (
    DEFAULT_KEEPALIVE_INTERVAL_S,
    FROZEN_PATH,
    SendStatus as ServerSendStatus,
    WssServer,
    WssServerConfig,
    WssServerStats,
    load_server_config,
)

__all__ = [
    # 常量
    "MAX_DOWNLINK_BYTES",
    "MAX_UPLINK_BYTES",
    "BACKOFF_SEQUENCE_S",
    "BACKOFF_JITTER_FRACTION",
    "BACKOFF_STABLE_RESET_S",
    "FROZEN_PATH",
    "DEFAULT_KEEPALIVE_INTERVAL_S",
    # 服务端
    "WssServer",
    "WssServerConfig",
    "WssServerStats",
    "ServerSendStatus",
    "load_server_config",
    # 客户端（mock 设备）
    "MockDeviceClient",
    "WssClientConfig",
    "ClientStats",
    "LinkState",
    "SendStatus",
    "load_client_config",
    # 链路语义
    "BackoffConfig",
    "BackoffPolicy",
    "FailureKind",
    "classify_handshake_status",
    "classify_close_code",
    "classify_exception",
    "connection_close_code",
    "is_config_error",
    "token_fingerprint",
    "TransportConfigError",
    "TlsVerificationError",
]
