"""P3.3 单元断言：冻结退避序列、失败分类、token 指纹、冻结常量。

纯逻辑（无 IO）：退避策略用注入 RNG，冻结值 1/2/4/8/16/30s±20%、≥60s 稳定
重置直接对默认配置断言（protocol/transport.md §5.4）。
"""

from __future__ import annotations

import logging
import random
import ssl

import pytest
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from bridge.transports.wss.link import (
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
    is_config_error,
    token_fingerprint,
)
from bridge.transports.wss.server import FROZEN_PATH
from websockets.datastructures import Headers


# ---------------------------------------------------------------------------
# 冻结常量锁定
# ---------------------------------------------------------------------------


def test_frozen_constants():
    assert BACKOFF_SEQUENCE_S == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    assert BACKOFF_JITTER_FRACTION == 0.2
    assert BACKOFF_STABLE_RESET_S == 60.0
    assert MAX_DOWNLINK_BYTES == 16384
    assert MAX_UPLINK_BYTES == 512
    assert FROZEN_PATH == "/v1/state"
    assert BackoffConfig().sequence_s == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


# ---------------------------------------------------------------------------
# 退避策略
# ---------------------------------------------------------------------------


def test_backoff_frozen_sequence_with_jitter_bounds():
    """连续失败：基础档位 1/2/4/8/16/30 → 封顶 30s，每档落在 ±20% 抖动内。"""
    policy = BackoffPolicy(rng=random.Random(42))
    expected_bases = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    for i, base in enumerate(expected_bases):
        delay = policy.next_delay()
        assert base * 0.8 <= delay <= base * 1.2, f"第 {i + 1} 次失败 delay={delay} base={base}"
    assert policy.attempts == len(expected_bases)


def test_backoff_jitter_spread_is_uniform_bounded():
    """大量采样下抖动严格落在 [0.8×, 1.2×]，且能触及两个边界附近。"""
    policy = BackoffPolicy(rng=random.Random(7))
    lows, highs = [], []
    for _ in range(500):
        policy.reset()
        d = policy.next_delay()  # base=1s 档
        assert 0.8 <= d <= 1.2
        lows.append(d)
        highs.append(d)
    assert min(lows) < 0.83 and max(highs) > 1.17  # 抖动真实生效


def test_backoff_stable_reset_after_60s():
    """≥60s 稳定连接重置回 1s 档；59.9s 不重置（transport.md §5.4）。"""
    policy = BackoffPolicy(rng=random.Random(1))
    for _ in range(4):
        policy.next_delay()  # 推进到第 5 档（16s）
    assert policy.attempts == 4

    assert policy.record_stable(59.9) is False
    delay = policy.next_delay()  # attempts=4 → 第 5 档 base=16s
    assert 16 * 0.8 <= delay <= 16 * 1.2  # 未重置：档位继续推进

    assert policy.record_stable(60.0) is True
    assert policy.attempts == 0
    delay = policy.next_delay()
    assert 0.8 <= delay <= 1.2  # 重置后回到 1s 档


def test_backoff_manual_reset():
    policy = BackoffPolicy(rng=random.Random(3))
    for _ in range(6):
        policy.next_delay()
    policy.reset()
    assert policy.attempts == 0
    assert 0.8 <= policy.next_delay() <= 1.2


def test_backoff_config_validation():
    with pytest.raises(TransportConfigError):
        BackoffConfig(sequence_s=())
    with pytest.raises(TransportConfigError):
        BackoffConfig(sequence_s=(1.0, 1.0, 2.0))  # 非严格递增
    with pytest.raises(TransportConfigError):
        BackoffConfig(sequence_s=(1.0, 0.0))
    with pytest.raises(TransportConfigError):
        BackoffConfig(jitter_fraction=-0.1)
    with pytest.raises(TransportConfigError):
        BackoffConfig(jitter_fraction=1.5)
    with pytest.raises(TransportConfigError):
        BackoffConfig(stable_reset_s=0)
    # 合法边界：jitter=0（无抖动）与 [0,1] 内任意值可正常构造
    BackoffConfig(jitter_fraction=0.0)
    BackoffConfig(jitter_fraction=0.5)


# ---------------------------------------------------------------------------
# 失败分类（transport.md §5.3 错误码表）
# ---------------------------------------------------------------------------


def _invalid_status(code: int) -> InvalidStatus:
    return InvalidStatus(Response(code, "test", Headers()))


def test_classify_handshake_status():
    assert classify_handshake_status(401) is FailureKind.AUTH_REJECTED
    assert classify_handshake_status(403) is FailureKind.POLICY_REJECTED
    assert classify_handshake_status(500) is FailureKind.RETRYABLE
    assert classify_handshake_status(503) is FailureKind.RETRYABLE
    assert classify_handshake_status(404) is FailureKind.RETRYABLE  # 未列明 → 可重试


def test_classify_close_code():
    assert classify_close_code(1008) is FailureKind.AUTH_STATE_LOST
    for code in (1000, 1006, 1009, 1011):
        assert classify_close_code(code) is FailureKind.RETRYABLE, code
    assert classify_close_code(None) is FailureKind.RETRYABLE  # 无 close frame = 1006 类


def test_classify_exception_tls_and_network():
    assert classify_exception(TlsVerificationError("spki mismatch")) is FailureKind.CERT_MISMATCH
    assert (
        classify_exception(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        is FailureKind.CERT_MISMATCH
    )
    assert classify_exception(_invalid_status(401)) is FailureKind.AUTH_REJECTED
    assert classify_exception(_invalid_status(503)) is FailureKind.RETRYABLE
    assert classify_exception(ConnectionRefusedError()) is FailureKind.RETRYABLE
    assert classify_exception(TimeoutError()) is FailureKind.RETRYABLE


def test_config_error_kinds_are_terminal():
    for kind in (
        FailureKind.AUTH_REJECTED,
        FailureKind.POLICY_REJECTED,
        FailureKind.CERT_MISMATCH,
        FailureKind.AUTH_STATE_LOST,
    ):
        assert is_config_error(kind)
    assert not is_config_error(FailureKind.RETRYABLE)


# ---------------------------------------------------------------------------
# token 指纹（凭证不入日志）
# ---------------------------------------------------------------------------


def test_token_fingerprint_shape():
    fp = token_fingerprint("s3cret-device-token")
    assert len(fp) == 8
    int(fp, 16)  # 8 hex
    assert fp == token_fingerprint("s3cret-device-token")  # 确定性
    assert fp != token_fingerprint("s3cret-device-tokenu")  # 区分不同 token
    assert "s3cret" not in fp  # 原文不可回传


def test_bearer_header_construction():
    from bridge.transports.wss.link import bearer_bytes, constant_time_eq

    assert constant_time_eq(bearer_bytes("tok"), b"Bearer tok")
    assert not constant_time_eq(bearer_bytes("tok"), b"Bearer to")
    assert not constant_time_eq(bearer_bytes("tok"), b"bearer tok")
