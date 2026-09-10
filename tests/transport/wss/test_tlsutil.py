"""P3.3 单元断言：SPKI SHA-256 解析与传输配置校验（fail-closed 规则）。

- SPKI 提取与 `openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER
  | openssl dgst -sha256` 交叉验证（证书经 scripts/gen_dev_certs.sh 生成）。
- 服务端/客户端配置违宪必须拒绝启动（transport.md §5.1/§5.5：生产禁明文、
  wss 必须 CA + SPKI pinning、路径冻结）。
"""

from __future__ import annotations

import ssl

import pytest

from bridge.transports.wss.link import TlsVerificationError, TransportConfigError
from bridge.transports.wss.tlsutil import (
    build_client_ssl_context,
    build_server_ssl_context,
    pem_to_der,
    spki_sha256_hex,
    spki_sha256_of_file,
    verify_spki_sha256_hex,
)
from bridge.transports.wss.server import WssServerConfig
from bridge.transports.wss.client_mock import WssClientConfig
from conftest import DEFAULT_TOKEN, make_app_state


# ---------------------------------------------------------------------------
# SPKI SHA-256
# ---------------------------------------------------------------------------


def test_spki_matches_openssl(dev_certs):
    """自研 DER 读取器与 openssl 输出逐字一致；脚本落盘文件一致。"""
    assert spki_sha256_of_file(dev_certs["cert"]) == dev_certs["spki"]
    der = pem_to_der(open(dev_certs["cert"], "rb").read())
    assert spki_sha256_hex(der) == dev_certs["spki"]


def test_spki_works_on_ca_cert_too(dev_certs):
    # 对任意证书（含 CA）都能稳定解析（无异常即结构正确）
    value = spki_sha256_of_file(dev_certs["ca"])
    assert len(value) == 64


def test_verify_spki_mismatch_raises_terminal(dev_certs):
    # 真实证书结构 + 错误指纹 → TlsVerificationError（CONFIG_ERROR 终态）
    der = pem_to_der(open(dev_certs["cert"], "rb").read())
    with pytest.raises(TlsVerificationError):
        verify_spki_sha256_hex(der, "ab" * 32)
    # 正确指纹通过
    verify_spki_sha256_hex(der, dev_certs["spki"])


def test_verify_spki_corrupt_der_raises():
    with pytest.raises(ValueError):
        spki_sha256_hex(b"not-a-cert")


# ---------------------------------------------------------------------------
# 服务端配置校验（fail-closed）
# ---------------------------------------------------------------------------


def test_server_rejects_plaintext_without_dev_flag():
    with pytest.raises(TransportConfigError, match="TLS"):
        WssServerConfig(host="127.0.0.1", port=8765).validate()


def test_server_rejects_plaintext_off_loopback():
    with pytest.raises(TransportConfigError, match="loopback"):
        WssServerConfig(host="192.168.1.10", port=8765, allow_insecure_loopback=True).validate()


def test_server_allows_dev_loopback_plaintext():
    for host in ("127.0.0.1", "::1", "localhost"):
        WssServerConfig(host=host, port=8765, allow_insecure_loopback=True).validate()


def test_server_rejects_non_frozen_path():
    with pytest.raises(TransportConfigError, match="/v1/state"):
        WssServerConfig(port=8765, path="/v1/other", allow_insecure_loopback=True).validate()


def test_server_requires_cert_key_pair(dev_certs):
    with pytest.raises(TransportConfigError, match="成对"):
        WssServerConfig(port=8765, tls_cert_file=dev_certs["cert"]).validate()
    with pytest.raises(TransportConfigError, match="成对"):
        WssServerConfig(port=8765, tls_key_file=dev_certs["key"]).validate()


def test_server_build_ssl_context_rejects_missing_file():
    with pytest.raises(TransportConfigError):
        build_server_ssl_context("/nonexistent/server.crt", "/nonexistent/server.key")


# ---------------------------------------------------------------------------
# 客户端（mock 设备）配置校验
# ---------------------------------------------------------------------------


def test_client_rejects_plaintext_without_dev_flag():
    with pytest.raises(TransportConfigError, match="dev_insecure_loopback"):
        WssClientConfig(url="ws://127.0.0.1:8765/v1/state").validate()


def test_client_rejects_plaintext_off_loopback():
    with pytest.raises(TransportConfigError, match="loopback"):
        WssClientConfig(
            url="ws://192.168.1.10:8765/v1/state", dev_insecure_loopback=True
        ).validate()


def test_client_wss_requires_ca_and_spki():
    with pytest.raises(TransportConfigError, match="ca_file"):
        WssClientConfig(url="wss://127.0.0.1:8765/v1/state").validate()
    with pytest.raises(TransportConfigError, match="spki_sha256_hex"):
        WssClientConfig(url="wss://127.0.0.1:8765/v1/state", ca_file="/tmp/ca.pem").validate()


def test_client_rejects_unknown_scheme():
    with pytest.raises(TransportConfigError, match="ws/wss"):
        WssClientConfig(url="http://127.0.0.1:8765/v1/state", dev_insecure_loopback=True).validate()


def test_client_wss_valid_with_ca_and_spki(dev_certs):
    WssClientConfig(
        url="wss://localhost:8765/v1/state",
        ca_file=dev_certs["ca"],
        spki_sha256_hex=dev_certs["spki"],
    ).validate()


def test_client_rejects_empty_token(dev_certs):
    from bridge.transports.wss.client_mock import MockDeviceClient

    with pytest.raises(TransportConfigError, match="token"):
        MockDeviceClient(
            WssClientConfig(
                url="wss://localhost:8765/v1/state",
                ca_file=dev_certs["ca"],
                spki_sha256_hex=dev_certs["spki"],
            ),
            device_token="",
        )


# ---------------------------------------------------------------------------
# 快照夹具自检（构造器产出的字节即协议形状）
# ---------------------------------------------------------------------------


def test_fixture_snapshot_is_valid_json_within_limits():
    data = make_app_state(seq=1)
    import json

    snap = json.loads(data.decode("utf-8"))
    assert snap["kind"] == "state"
    assert snap["seq"] == 1
    assert len(data) <= 16384
