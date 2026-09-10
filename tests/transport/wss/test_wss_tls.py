"""P3.3 TLS loopback 断言（自签证书 + SPKI pinning；transport.md §5.5）。

证书由 scripts/gen_dev_certs.sh 生成（SAN: localhost/127.0.0.1/::1）：
- 正确 CA + 正确 SPKI pinning → 连接成功、首帧全量；
- 错误 CA → TLS 验证失败 = CONFIG_ERROR 终态不重试；
- 正确 CA + 错误 SPKI 指纹 → CONFIG_ERROR 终态不重试。
"""

from __future__ import annotations

import asyncio

from conftest import (
    DEFAULT_TOKEN,
    FrameSink,
    make_app_state,
    make_client,
    start_server,
    stop_client,
    wait_link,
    wss_url,
)
from bridge.transports.wss import LinkState


def run(coro):
    return asyncio.run(coro)


def test_tls_pinned_client_receives_full_snapshot(dev_certs):
    """正确 CA + SPKI pinning → wss 连接成功，首条 text message = 完整快照。"""

    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap, tls=(dev_certs["cert"], dev_certs["key"]))
        try:
            client = make_client(
                wss_url(server),
                sink=sink,
                ca_file=dev_certs["ca"],
                spki=dev_certs["spki"],
            )
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await stop_client(client, task)
            assert sink.frames[0] == snap
            assert client.stats.config_errors == 0
            assert client.stats.messages_received == 1
        finally:
            await server.stop()

    run(scenario())


def test_tls_localhost_hostname_with_pinning(dev_certs):
    """wss://localhost + 证书 SAN（DNS:localhost）主机名校验通过。"""

    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap, tls=(dev_certs["cert"], dev_certs["key"]))
        try:
            client = make_client(
                wss_url(server, hostname="localhost"),
                sink=sink,
                ca_file=dev_certs["ca"],
                spki=dev_certs["spki"],
            )
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await stop_client(client, task)
            assert sink.frames[0] == snap
        finally:
            await server.stop()

    run(scenario())


def test_tls_wrong_ca_is_config_error_no_retry(dev_certs, alt_certs):
    """错误 CA → TLS 验证失败 → CONFIG_ERROR 终态：attempts=1、无退避重试。"""

    async def scenario():
        server = await start_server(
            snapshot=make_app_state(seq=1), tls=(dev_certs["cert"], dev_certs["key"])
        )
        try:
            client = make_client(
                wss_url(server),
                ca_file=alt_certs["ca"],  # 独立 CA：验链必败
                spki=dev_certs["spki"],
            )
            task = asyncio.create_task(client.run())
            await wait_link(client, LinkState.CONFIG_ERROR)
            assert task.done()
            await asyncio.wait_for(task, timeout=5)
            assert client.stats.attempts == 1, "证书失败为终态，重试计数必须为 0"
            assert client.stats.retry_delays_s == []
            assert client.stats.config_errors == 1
            assert client.stats.messages_received == 0
            assert any(k == "cert_or_spki_mismatch" for k in client.stats.failure_kinds)
        finally:
            await server.stop()

    run(scenario())


def test_tls_wrong_spki_pin_is_config_error_no_retry(dev_certs):
    """正确 CA 但 SPKI 指纹不符 → pinning 失败 → CONFIG_ERROR 终态。"""

    async def scenario():
        server = await start_server(
            snapshot=make_app_state(seq=1), tls=(dev_certs["cert"], dev_certs["key"])
        )
        try:
            wrong_spki = "ab" * 32
            client = make_client(
                wss_url(server),
                ca_file=dev_certs["ca"],
                spki=wrong_spki,
            )
            task = asyncio.create_task(client.run())
            await wait_link(client, LinkState.CONFIG_ERROR)
            assert task.done()
            await asyncio.wait_for(task, timeout=5)
            assert client.stats.attempts == 1
            assert client.stats.retry_delays_s == []
            assert client.stats.config_errors == 1
            assert any(k == "cert_or_spki_mismatch" for k in client.stats.failure_kinds)
        finally:
            await server.stop()

    run(scenario())


def test_tls_bridge_shutdown_close_1000_over_wss(dev_certs):
    """wss 下的 Bridge 正常停机同样发送 close 1000。"""
    from conftest import wait_for

    async def scenario():
        server = await start_server(
            snapshot=make_app_state(seq=1), tls=(dev_certs["cert"], dev_certs["key"])
        )
        try:
            sink = FrameSink()
            client = make_client(
                wss_url(server), sink=sink, ca_file=dev_certs["ca"], spki=dev_certs["spki"]
            )
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await server.stop()
            await wait_for(
                lambda: 1000 in [c for c in client.stats.close_codes if c is not None],
                timeout=5,
                what="wss 客户端收到 close 1000",
            )
            await stop_client(client, task)
        finally:
            await server.stop()

    run(scenario())
