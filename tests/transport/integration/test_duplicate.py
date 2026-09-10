"""P3.5 故障矩阵·重复（D1–D3）：同快照重复送达 → StateStore 仅应用一次。

验收锚点（DEVELOPMENT_PLAN P3.5 行 + INTERFACES §3 seq 行 / §4 / §7）：
  - 终点收敛：StateStore 同 epoch seq<=last → IGNORED_STALE_SEQ，状态不变；
  - UI 侧快照 seq 不回退：harness 内真实 presenter 判定 ui_no_regress；
  - 连接状态可观测：link 事件序列记录在报告（本路径无断链 → 恒 connected）。

D1 WSS：服务端重发同 epoch 同 seq、不同字节的快照（字节不同才会被服务端
  去重放行）→ 传输两次送达、store 仅应用一次。
D2 WSS：逐字节相同重发 → 服务端按字节去重（第一道防线），设备端零扰动。
D3 BLE：整消息全部分片重发 → 重组两次、store 第二次 IGNORED_STALE_SEQ、
  两次都 ACK（§7：重复「ACK 但不再渲染」）。
"""

from __future__ import annotations

import asyncio
import os

from bridge.transports.ble.fragmenter import Fragmenter

import p35_common as pc
import wss_driver as wd


# ---------------------------------------------------------------------------
# D1：WSS 服务端重发同 seq 快照
# ---------------------------------------------------------------------------


async def _d1_amain(harness_bin: str) -> None:
    case = "D1_wss_dup_same_seq_redelivery"
    d = pc.case_dir(case)
    a = pc.make_state(epoch="run-1", seq=7, activity="first-apply")
    b = pc.make_state(epoch="run-1", seq=7, activity="second-copy-same-seq")

    sink, links = wd.FrameSink(), wd.LinkLog()
    server = await wd.start_server()
    client = wd.make_client(wd.ws_url(server), sink=sink, links=links)
    task = asyncio.create_task(client.run())
    try:
        await wd.wait_connected(client)
        await wd.send_snapshot(server, a)
        await wd.send_snapshot(server, b)
        await sink.wait_count(2)
        assert sink.frames[0] == a and sink.frames[1] == b, "服务端应把两份字节都送达"
    finally:
        await wd.stop_client(client, task)
        await wd.stop_server(server)

    report = pc.run_case(case, 53, "wifi", [
        pc.snap(0, a),
        pc.snap(1000, b),   # 同 epoch 同 seq、不同字节 → 传输层再次送达
        pc.probe(2000),
    ], binary=harness_bin)

    rounds = report["rounds"]
    assert rounds[0]["apply"] == "applied" and rounds[0]["seq"] == 7, rounds[0]
    assert rounds[1]["apply"] == "ignored_stale_seq", "同 seq 重复必须被 store 丢弃"
    assert rounds[2]["view"]["activity"] == "first-apply", "重复送达后 UI 必须保持首次内容"
    assert pc.applied_pairs(report) == [("run-1", 7)], "store 仅应用一次"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)

    pc.dump_json(os.path.join(d, "wss_trace.json"), {
        "messages_received": client.stats.messages_received,
        "server_pushed": server.stats.snapshots_pushed,
        "link_states": links.states(),
    })
    print("[D1] PASS：WSS 同 seq 重发两次送达，store 仅应用一次，view 保持首份内容")


def test_d1_wss_dup_same_seq(harness_bin: str) -> None:
    asyncio.run(_d1_amain(harness_bin))


# ---------------------------------------------------------------------------
# D2：WSS 逐字节相同重发（服务端去重 = 第一道防线）
# ---------------------------------------------------------------------------


async def _d2_amain(harness_bin: str) -> None:
    case = "D2_wss_dup_identical_bytes_server_dedup"
    d = pc.case_dir(case)
    a = pc.make_state(epoch="run-1", seq=3, activity="identical")

    sink, links = wd.FrameSink(), wd.LinkLog()
    server = await wd.start_server()
    client = wd.make_client(wd.ws_url(server), sink=sink, links=links)
    task = asyncio.create_task(client.run())
    try:
        await wd.wait_connected(client)
        await wd.send_snapshot(server, a)
        await sink.wait_count(1)
        await wd.send_snapshot(server, a)  # 相同字节 → 去重
        await asyncio.sleep(0.3)
        assert len(sink.frames) == 1, "相同字节重发不得产生第二帧（服务端去重）"
        assert server.stats.snapshots_deduped >= 1
    finally:
        await wd.stop_client(client, task)
        await wd.stop_server(server)

    report = pc.run_case(case, 53, "wifi", [pc.snap(0, a)], binary=harness_bin)
    assert pc.applied_pairs(report) == [("run-1", 3)]
    assert report["ui_no_regress"]
    pc.dump_json(os.path.join(d, "wss_trace.json"), {
        "messages_received": client.stats.messages_received,
        "server_pushed": server.stats.snapshots_pushed,
        "server_deduped": server.stats.snapshots_deduped,
        "link_states": links.states(),
    })
    print("[D2] PASS：相同字节服务端去重；设备端仅一次应用")


def test_d2_wss_dup_identical_bytes(harness_bin: str) -> None:
    asyncio.run(_d2_amain(harness_bin))


# ---------------------------------------------------------------------------
# D3：BLE 全部分片重发 → 重复应用被拒 + ACK
# ---------------------------------------------------------------------------


def test_d3_ble_dup_full_fragment_resend(harness_bin: str) -> None:
    case = "D3_ble_dup_full_fragment_resend"
    payload = pc.make_state(epoch="run-1", seq=9, activity="dup-via-ble")
    fg = Fragmenter(payload, message_id=42, mtu=53)
    frames = list(fg.frames())

    entries = [pc.frag(100 * i, f) for i, f in enumerate(frames)]
    entries += [pc.frag(5000 + 100 * i, f) for i, f in enumerate(frames)]  # 全片重发

    report = pc.run_case(case, 53, "ble", entries, binary=harness_bin)
    rounds = report["rounds"]
    completed = [r for r in rounds if r["reassemble"] == "completed"]
    assert len(completed) == 2, "两次全片投递都应完成重组"
    assert completed[0]["apply"] == "applied" and completed[0]["seq"] == 9
    assert completed[1]["apply"] == "ignored_stale_seq", "重发的旧 seq 必须被 store 丢弃"
    assert completed[0]["ack"] == "ack" and completed[1]["ack"] == "ack", \
        "§7：applied 与 duplicate 都回 ACK（重复 ACK 但不再渲染）"
    assert pc.applied_pairs(report) == [("run-1", 9)], "store 仅应用一次"
    assert report["ui_no_regress"], report["no_regress_notes"]
    assert rounds[-1]["view"]["activity"] == "dup-via-ble"
    pc.assert_link_trace(report, [(0, "connected")], case)
    print("[D3] PASS：BLE 全片重发重组两次、应用一次、双 ACK，view 无回退")
