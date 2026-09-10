"""P3.3 loopback 全链路断言（server + client_mock，本机端口，无硬件）。

对应 DEVELOPMENT_PLAN P3.3 验收行「断网重连取得最新快照；拒绝未授权连接
及超大帧」与 transport.md §7.1 步骤 W0–W4 的 loopback 部分：

- 认证成功收首帧全量（首条 text message = 完整快照字节）；
- 错 token → HTTP 401 → 设备侧 CONFIG_ERROR 终态、重试计数 0；
- 上行 >512 字节 → 服务端 close 1009；下行 >16384 服务端拒绝发送（Bridge 缺陷计数）；
- 服务端重启 → 客户端退避重连后取得最新全量（不是旧的）；
- 同 epoch seq 单调；两连接同快照字节一致（BLE/Wi-Fi 同 JSON 的 Wi-Fi 侧证据）。

注意：Transport 断言只针对字节/计数/链路状态；seq/epoch 的 JSON 字段检查
发生在测试"验证者"一侧，不进入被测代码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from bridge.transports.wss import LinkState, WssServer, WssServerConfig
from bridge.transports.wss.link import connection_close_code
from conftest import (
    DEFAULT_TOKEN,
    FrameSink,
    LinkLog,
    make_app_state,
    make_client,
    start_server,
    stop_client,
    wait_connected,
    wait_for,
    wait_link,
    ws_url,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 认证与首帧（W0/W1）
# ---------------------------------------------------------------------------


def test_auth_success_receives_full_snapshot_first_frame():
    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await stop_client(client, task)
            assert sink.frames[0] == snap, "首条 text message 必须是完整快照字节"
            assert client.stats.messages_received == 1
            assert client.stats.config_errors == 0
        finally:
            await server.stop()

    run(scenario())


def test_first_frame_is_schema_valid_app_state():
    """首帧字节按业务 schema 校验（验证者一侧；证明字节是合法 AppState JSON）。"""
    jsonschema = pytest.importorskip("jsonschema")
    from conftest import REPO_ROOT

    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await stop_client(client, task)
        finally:
            await server.stop()
        first = sink.frames[0]
        schema = json.loads(
            (REPO_ROOT / "protocol" / "state.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(json.loads(first.decode("utf-8")))

    run(scenario())


def test_wrong_token_401_config_error_no_retry(caplog):
    """错 token → 401 → 设备 CONFIG_ERROR 终态：attempts=1、退避计数 0。"""
    token_fp = hashlib.sha256(b"wrong-token").hexdigest()[:8]

    async def scenario():
        snap = make_app_state(seq=1)
        server = await start_server(snapshot=snap)
        try:
            links = LinkLog()
            client = make_client(ws_url(server), token="wrong-token", links=links)
            task = asyncio.create_task(client.run())
            await wait_link(client, LinkState.CONFIG_ERROR)
            assert task.done(), "CONFIG_ERROR 终态后 run() 必须结束"
            await asyncio.wait_for(task, timeout=5)

            assert client.stats.attempts == 1
            assert client.stats.retry_delays_s == []
            assert client.stats.config_errors == 1
            assert client.stats.messages_received == 0
            assert server.stats.rejected_auth_401 == 1
            assert any(state is LinkState.CONFIG_ERROR for state, _ in links.events)
        finally:
            await server.stop()

    with caplog.at_level(logging.INFO, logger="cdt.transport.wss.server"):
        run(scenario())
    # 凭证红线：日志只有指纹，无 token 原文
    assert "wrong-token" not in caplog.text
    assert token_fp in caplog.text


def test_wrong_token_terminal_stays_after_reset_only():
    """CONFIG_ERROR 后 reset()（人工介入）才允许重试；直接复跑不重试。"""

    async def scenario():
        server = await start_server(snapshot=make_app_state(seq=1))
        try:
            client = make_client(ws_url(server), token="wrong-token")
            task = asyncio.create_task(client.run())
            await wait_link(client, LinkState.CONFIG_ERROR)
            # 直接再次运行：终态保持，不产生新尝试
            state = await client.run()
            assert client.stats.attempts == 1
            # reset（配置变更/人工介入）后允许重新尝试
            client.reset()
            state = await asyncio.wait_for(client.run(), timeout=5)
            assert client.stats.attempts == 2
            assert state.value in ("config_error", "stopped")
            await asyncio.wait_for(task, timeout=5)
        finally:
            await server.stop()

    run(scenario())


def _link_state(name):
    from bridge.transports.wss import LinkState

    return LinkState[name]


def test_missing_token_gets_401():
    async def scenario():
        server = await start_server(snapshot=make_app_state(seq=1))
        try:
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(ws_url(server)):
                    pass
            assert excinfo.value.response.status_code == 401
            assert server.stats.rejected_auth_401 == 1
        finally:
            await server.stop()

    run(scenario())


def test_wrong_path_gets_404():
    async def scenario():
        server = await start_server(snapshot=make_app_state(seq=1))
        try:
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(f"ws://127.0.0.1:{server.bound_port}/v1/other",
                                   additional_headers={"Authorization": f"Bearer {DEFAULT_TOKEN}"}):
                    pass
            assert excinfo.value.response.status_code == 404
            assert server.stats.rejected_path_404 == 1
        finally:
            await server.stop()

    run(scenario())


def test_policy_403_when_client_slot_full():
    """已连设备数达上限：第二个连接 upgrade 被 403 拒绝（策略 → CONFIG_ERROR 类）。"""
    from bridge.transports.wss import FailureKind
    from bridge.transports.wss.link import classify_handshake_status

    async def scenario():
        sink1 = FrameSink()
        server = await start_server(snapshot=make_app_state(seq=1), max_clients=1)
        try:
            client1 = make_client(ws_url(server), sink=sink1)
            task1 = asyncio.create_task(client1.run())
            await wait_connected(client1)
            assert server.client_count == 1

            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(ws_url(server),
                                   additional_headers={"Authorization": f"Bearer {DEFAULT_TOKEN}"}):
                    pass
            assert excinfo.value.response.status_code == 403
            assert classify_handshake_status(403) is FailureKind.POLICY_REJECTED
            assert server.stats.rejected_policy_403 == 1
            # 已连客户端不受影响
            assert server.client_count == 1
            await stop_client(client1, task1)
        finally:
            await server.stop()

    run(scenario())


# ---------------------------------------------------------------------------
# 尺寸上限（W3）
# ---------------------------------------------------------------------------


def test_oversize_uplink_closed_1009():
    """上行 >512 字节 → 服务端 close 1009（websockets max_size 强制）。"""
    from bridge.transports.wss import MAX_UPLINK_BYTES

    async def scenario():
        uplink = FrameSink()  # 复用为服务端 on_message 收集器
        server = await start_server(snapshot=make_app_state(seq=1), on_message=uplink)
        try:
            async with connect(ws_url(server),
                               additional_headers={"Authorization": f"Bearer {DEFAULT_TOKEN}"}) as raw:
                await raw.send("x" * (MAX_UPLINK_BYTES + 88))
                with pytest.raises(ConnectionClosed) as excinfo:
                    while True:
                        await asyncio.wait_for(raw.recv(), timeout=5)
            assert connection_close_code(excinfo.value) == 1009
            assert server.stats.uplink_messages == 0
        finally:
            await server.stop()

    run(scenario())


def test_uplink_within_limit_is_passed_through():
    """上行 ≤512 字节遥测字节原样透传（Transport 不读取内容）。"""

    async def scenario():
        uplink = FrameSink()
        sink = FrameSink()
        server = await start_server(snapshot=make_app_state(seq=1), on_message=uplink)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await wait_connected(client)
            payload = json.dumps(
                {"schema_version": 1, "kind": "telemetry", "battery_mv": 3700,
                 "battery_valid": True, "rssi_dbm": -52, "transport": "wifi",
                 "sent_at_ms": None},
                separators=(",", ":"),
            ).encode("utf-8")
            status = await client.send_telemetry(payload)
            assert status.value == "accepted"
            await uplink.wait_count(1)
            got = uplink.frames[0]
            await stop_client(client, task)
            assert got == payload
            assert server.stats.uplink_messages == 1
            assert server.stats.uplink_bytes == len(payload)
        finally:
            await server.stop()

    run(scenario())


def test_client_telemetry_over_512_rejected_locally():
    from bridge.transports.wss import MAX_UPLINK_BYTES

    async def scenario():
        uplink = FrameSink()
        server = await start_server(snapshot=make_app_state(seq=1), on_message=uplink)
        try:
            client = make_client(ws_url(server))
            task = asyncio.create_task(client.run())
            await wait_connected(client)
            status = await client.send_telemetry(b"x" * (MAX_UPLINK_BYTES + 1))
            assert status.value == "error"
            assert server.stats.uplink_messages == 0
            await stop_client(client, task)
        finally:
            await server.stop()

    run(scenario())


def test_downlink_oversize_snapshot_never_sent():
    """>16384 字节下行 = Bridge 缺陷：服务端拒绝发送并计数；连接保持。"""
    from bridge.transports.wss import MAX_DOWNLINK_BYTES

    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await wait_connected(client)
            await sink.wait_count(1)  # 首帧

            status = await server.send(b"z" * (MAX_DOWNLINK_BYTES + 1))
            assert status.value == "error"
            assert server.stats.oversize_out_rejected == 1

            # 缺陷帧不发出；客户端保持连接并还能收到后续合法快照
            snap2 = make_app_state(seq=2)
            assert (await server.send(snap2)).value == "accepted"
            got = await sink.next()
            await stop_client(client, task)
            assert got == snap2
            assert client.stats.oversize_rejected == 0
        finally:
            await server.stop()

    run(scenario())


def test_client_downlink_oversize_from_peer_closes_1009_and_counts():
    """缺陷 Bridge 直发 >16384 字节下行 → 客户端库 max_size close 1009：
    计数 oversize_rejected=1、on_message 不交付、按可重试退避（§5.3 设备侧行）。"""
    from bridge.transports.wss import MAX_DOWNLINK_BYTES
    from websockets.asyncio.server import serve as raw_serve

    async def scenario():
        bad_frames = []

        async def bad_handler(conn):
            await conn.send("z" * (MAX_DOWNLINK_BYTES + 100))  # Bridge 缺陷：超限直发
            await conn.wait_closed()

        raw_server = await raw_serve(bad_handler, "127.0.0.1", 0)
        try:
            port = raw_server.sockets[0].getsockname()[1]
            sink = FrameSink()
            client = make_client(f"ws://127.0.0.1:{port}/v1/state", sink=sink)
            task = asyncio.create_task(client.run())
            await wait_for(
                lambda: client.stats.oversize_rejected >= 1,
                timeout=5,
                what="客户端 1009 计数（聚合超限）",
            )
            await stop_client(client, task)
            assert sink.frames == [], "超限帧绝不交付 on_message（不解析不应用）"
            assert 1009 in [c for c in client.stats.close_codes if c is not None]
            assert client.stats.retry_delays_s, "1009 属可重试类：必须进入退避"
            assert client.stats.config_errors == 0
        finally:
            raw_server.close()
            await raw_server.wait_closed()

    run(scenario())


# ---------------------------------------------------------------------------
# 推送、心跳快照、seq 单调（W0）
# ---------------------------------------------------------------------------


def test_push_on_publish_same_epoch_seq_monotonic():
    async def scenario():
        snap1 = make_app_state(epoch="mock-run-001", seq=1)
        snap2 = make_app_state(epoch="mock-run-001", seq=2)
        snap3 = make_app_state(epoch="mock-run-001", seq=3)
        sink = FrameSink()
        server = await start_server(snapshot=snap1)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await server.send(snap2)
            await server.send(snap3)
            await sink.wait_count(3)
            await stop_client(client, task)
        finally:
            await server.stop()

        assert sink.frames == [snap1, snap2, snap3]
        seqs = [json.loads(f.decode("utf-8"))["seq"] for f in sink.frames]
        epochs = {json.loads(f.decode("utf-8"))["bridge_epoch"] for f in sink.frames}
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "同 epoch seq 必须严格递增"
        assert seqs == [1, 2, 3]
        assert epochs == {"mock-run-001"}

    run(scenario())


def test_identical_bytes_resend_is_deduped():
    async def scenario():
        snap = make_app_state(seq=1)
        sink = FrameSink()
        server = await start_server(snapshot=snap)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)
            await server.send(snap)  # 相同字节：不重复推送
            status = await server.send(snap)
            assert status.value == "accepted"
            await wait_for(lambda: server.stats.snapshots_deduped >= 2,
                           timeout=5, what="相同字节快照被去重计数")
            await stop_client(client, task)
            assert client.stats.messages_received == 1
        finally:
            await server.stop()

    run(scenario())


def test_keepalive_snapshot_push_and_dedupe():
    """存活快照：provider 给新字节 → 周期推送；字节未变 → 去重不发送。"""

    async def scenario():
        counter = {"seq": 0, "pinned": False}

        async def provider():
            if counter["pinned"]:
                return make_app_state(epoch="mock-run-001", seq=99)
            counter["seq"] += 1
            return make_app_state(epoch="mock-run-001", seq=counter["seq"])

        sink = FrameSink()
        server = await start_server(provider=provider, keepalive=0.1)
        try:
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await sink.wait_count(1)  # 首帧来自 provider（seq=1）
            await wait_for(lambda: len(sink.frames) >= 3, timeout=5,
                           what="存活快照周期推送（≥3 帧）")
            # 固定 provider 输出：第一个 pinned 周期会正常推送一次（字节变化），
            # 其后相同字节不再推送
            pinned = make_app_state(epoch="mock-run-001", seq=99)
            counter["pinned"] = True
            await wait_for(lambda: sink.frames and sink.frames[-1] == pinned,
                           timeout=5, what="pinned 快照首次推送")
            stable = len(sink.frames)
            await asyncio.sleep(0.5)
            await stop_client(client, task)
            assert len(sink.frames) == stable, "字节未变化的存活快照不得重复推送"
            assert server.stats.snapshots_deduped >= 1
        finally:
            await server.stop()

    run(scenario())


# ---------------------------------------------------------------------------
# 双连接字节一致（§8：BLE/Wi-Fi 同 JSON 的 Wi-Fi 侧证据）
# ---------------------------------------------------------------------------


def test_two_connections_receive_identical_snapshot_bytes():
    async def scenario():
        snap = make_app_state(seq=1)
        sink1, sink2 = FrameSink(), FrameSink()
        server = await start_server(snapshot=snap, max_clients=2)
        try:
            client1 = make_client(ws_url(server), sink=sink1)
            client2 = make_client(ws_url(server), sink=sink2)
            task1 = asyncio.create_task(client1.run())
            task2 = asyncio.create_task(client2.run())
            await sink1.wait_count(1)
            await sink2.wait_count(1)
            await stop_client(client1, task1)
            await stop_client(client2, task2)
        finally:
            await server.stop()

        assert sink1.frames[0] == snap
        assert sink2.frames[0] == snap
        assert (hashlib.sha256(sink1.frames[0]).hexdigest()
                == hashlib.sha256(sink2.frames[0]).hexdigest()), "两连接必须收到字节相同的快照"

    run(scenario())


# ---------------------------------------------------------------------------
# 断连重连（W4）：Bridge 停机 → 退避重连 → 最新全量
# ---------------------------------------------------------------------------


def test_server_restart_client_reconnects_gets_latest_snapshot():
    """kill 服务端 → 客户端退避重连 → 首帧是最新快照（新字节、新 epoch），不是旧的。"""

    async def scenario():
        snap_old = make_app_state(epoch="mock-run-001", seq=7)
        snap_new = make_app_state(epoch="mock-run-002", seq=1)

        async def provider_new():
            return snap_new

        sink = FrameSink()
        server_a = await start_server(snapshot=snap_old)
        port = server_a.bound_port
        client = make_client(ws_url(server_a), sink=sink)
        task = asyncio.create_task(client.run())
        try:
            await sink.wait_count(1)
            assert sink.frames[0] == snap_old

            await server_a.stop()  # Bridge 停机（close 1000）

            # 客户端进入退避重连循环；期间 Bridge 重启并给出新快照
            from bridge.transports.wss import WssServerConfig, WssServer

            cfg = WssServerConfig(host="127.0.0.1", port=port,
                                  keepalive_interval_s=None, allow_insecure_loopback=True)
            server_b = WssServer(cfg, device_token=DEFAULT_TOKEN, snapshot_provider=provider_new)
            await server_b.start()

            await wait_for(
                lambda: len(sink.frames) >= 2 and sink.frames[-1] == snap_new,
                timeout=10,
                what="重连后取得最新全量快照",
            )
            assert sink.frames == [snap_old, snap_new], "重连后首帧必须是最新快照，不得是旧帧"
            assert client.stats.attempts >= 2, "必须发生退避重连"
            assert client.stats.retry_delays_s, "必须有退避延迟记录"
            # 退避档位落在缩短序列 ±20% 内（形状与冻结序列一致）
            seq_test = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)
            for i, d in enumerate(client.stats.retry_delays_s):
                base = seq_test[min(i, len(seq_test) - 1)]
                assert base * 0.8 <= d <= base * 1.2, f"退避第 {i + 1} 档 {d} 超出 {base}±20%"

            await stop_client(client, task)
            await server_b.stop()
        except BaseException:
            task.cancel()
            raise

    run(scenario())


def test_bridge_shutdown_sends_close_1000():
    async def scenario():
        server = await start_server(snapshot=make_app_state(seq=1))
        try:
            sink = FrameSink()
            client = make_client(ws_url(server), sink=sink)
            task = asyncio.create_task(client.run())
            await wait_connected(client)
            await sink.wait_count(1)

            await server.stop()  # §5.3：Bridge 正常关闭 = close 1000
            await wait_for(
                lambda: 1000 in [c for c in client.stats.close_codes if c is not None],
                timeout=5,
                what="客户端收到 close 1000",
            )
            await stop_client(client, task)
            assert client.link_state.value in ("stopped", "disconnected")
        finally:
            await server.stop()

    run(scenario())


# ---------------------------------------------------------------------------
# send 生命周期语义（INTERFACES §5）
# ---------------------------------------------------------------------------


def test_send_before_start_returns_busy():
    async def scenario():
        cfg = WssServerConfig(port=0, keepalive_interval_s=None, allow_insecure_loopback=True)
        server = WssServer(cfg, device_token=DEFAULT_TOKEN)
        status = await server.send(make_app_state(seq=1))
        assert status.value == "busy"
        await server.stop()

    run(scenario())


def test_send_telemetry_when_disconnected_returns_error():
    """未连接时上行 = error；连接中（connecting）= busy 分支由 LinkState 决定。"""

    async def scenario():
        server = await start_server(snapshot=make_app_state(seq=1))
        try:
            client = make_client(ws_url(server))
            status = await client.send_telemetry(b"{}")
            assert status.value == "error"  # DISCONNECTED
        finally:
            await server.stop()

    run(scenario())
