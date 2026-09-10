"""P3.5 故障矩阵·超时（T1–T3）：半包超时丢弃 / 断连退避重连取最新全量。

验收锚点（DEVELOPMENT_PLAN P3.5 行 + INTERFACES §4/§7）：
  - BLE 半包 3s 无进展 → progress_timeout 丢弃（30s 整包阈值另测 T3）；
    丢弃后新消息正常应用（残留不复活）；
  - WSS 断连 → 冻结退避（测试用缩短序列，形状一致）→ 重连后取得最新全量
    （非旧帧）；
  - 连接状态可观测：Python 侧 LinkLog + harness §4 link 事件双份证据。
"""

from __future__ import annotations

import asyncio
import os

from bridge.transports.ble.fragmenter import Fragmenter
from bridge.transports.wss import LinkState

import p35_common as pc
import wss_driver as wd


# ---------------------------------------------------------------------------
# T1：BLE 半包 3s 无进展 → 丢弃，新消息正常
# ---------------------------------------------------------------------------


def test_t1_ble_half_message_progress_timeout(harness_bin: str) -> None:
    case = "T1_ble_half_message_progress_timeout"
    half_fg = Fragmenter(pc.make_state(epoch="run-1", seq=10, activity="half-A"),
                         message_id=200, mtu=53)
    fresh_fg = Fragmenter(pc.make_state(epoch="run-1", seq=11, activity="fresh-B"),
                          message_id=201, mtu=53)
    half_f, fresh_f = list(half_fg.frames()), list(fresh_fg.frames())

    entries = [
        pc.frag(0, half_f[0]),
        pc.frag(2500, half_f[1]),   # 距上次新片 2.5s < 3s：有进展
        pc.frag(8000, half_f[2]),   # 距上次新片 5.5s ≥ 3s：progress_timeout 丢弃
        pc.frag(8010, half_f[3]),   # 残留片：上下文已清 → 孤儿片拒绝
    ]
    entries += [pc.frag(9000 + 100 * i, f) for i, f in enumerate(fresh_f)]

    report = pc.run_case(case, 53, "ble", entries, binary=harness_bin)
    reasons = [r["reassemble"] for r in report["rounds"]]
    assert "rejected:progress_timeout" in reasons, "3s 无新片进展必须丢弃半包"
    assert "rejected:orphan" in reasons, "超时丢弃后的残留片不得复活"
    completed = [r for r in report["rounds"] if r["reassemble"] == "completed"]
    assert len(completed) == 1 and completed[0]["apply"] == "applied"
    assert completed[0]["seq"] == 11, "超时之后新消息必须正常应用"
    assert report["store_final"]["seq"] == 11
    assert report["rounds"][-1]["view"]["activity"] == "fresh-B"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    print("[T1] PASS：BLE 半包 progress_timeout 丢弃 + 孤儿片拒绝，新消息正常")


# ---------------------------------------------------------------------------
# T3：BLE 整包 30s 超时（片持续滴漏、无 3s 间隔 → total_timeout）
# ---------------------------------------------------------------------------


def test_t3_ble_total_timeout_30s(harness_bin: str) -> None:
    case = "T3_ble_total_timeout_30s"
    slow_fg = Fragmenter(pc.make_state(epoch="run-1", seq=20, activity="slow-C"),
                         message_id=300, mtu=53)
    fresh_fg = Fragmenter(pc.make_state(epoch="run-1", seq=21, activity="fresh-D"),
                          message_id=301, mtu=53)
    slow_f, fresh_f = list(slow_fg.frames()), list(fresh_fg.frames())
    assert len(slow_f) >= 12, "场景需要足够分片把整包时长拖过 30s"

    entries = [pc.frag(2800 * i, f) for i, f in enumerate(slow_f[:11])]  # 滴漏，无 3s 空窗
    entries.append(pc.frag(30800, slow_f[11]))  # 整包 30.8s ≥ 30s → total_timeout
    entries += [pc.frag(31000 + 100 * i, f) for i, f in enumerate(fresh_f)]

    report = pc.run_case(case, 53, "ble", entries, binary=harness_bin)
    reasons = [r["reassemble"] for r in report["rounds"]]
    assert "rejected:total_timeout" in reasons, "整包 30s 未完成必须丢弃"
    completed = [r for r in report["rounds"] if r["reassemble"] == "completed"]
    assert len(completed) == 1 and completed[0]["apply"] == "applied"
    assert completed[0]["seq"] == 21
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    print("[T3] PASS：BLE 整包 30s 超时丢弃，后续新消息正常")


# ---------------------------------------------------------------------------
# T2：WSS 断连 → 冻结退避 → 重连取得最新全量（Bridge 重启拓扑：同端口）
# ---------------------------------------------------------------------------


async def _t2_amain(harness_bin: str) -> None:
    case = "T2_wss_disconnect_backoff_latest_full"
    d = pc.case_dir(case)
    holder = {"snap": pc.make_state(epoch="run-1", seq=1, activity="before-drop")}

    async def provider():
        return holder["snap"]

    sink, links = wd.FrameSink(), wd.LinkLog()
    server1 = await wd.start_server(provider=provider)
    port = server1.bound_port
    client = wd.make_client(wd.ws_url(server1), sink=sink, links=links)
    task = asyncio.create_task(client.run())
    try:
        await wd.wait_connected(client)
        await sink.wait_count(1)
        assert sink.frames[0] == holder["snap"], "连接后首帧必须是当时的全量快照"

        # 断连（close 1000，可重试类）+ 期间业务层已产出更新的快照
        holder["snap"] = pc.make_state(epoch="run-1", seq=2, activity="after-drop-latest")
        await server1.stop()
        await wd.wait_link(client, LinkState.DISCONNECTED)

        # Bridge 同端口恢复（进程重启拓扑）；设备退避重连后必须取最新全量
        server2 = await wd.start_server(provider=provider, port=port)
        await wd.wait_connected(client, timeout=10.0)
        await sink.wait_count(2)
        assert sink.frames[1] == holder["snap"], "重连后必须收到最新全量快照（非旧帧）"
        assert len(client.stats.retry_delays_s) >= 1, "断连后必须经过退避序列"
        await wd.stop_client(client, task)
        await wd.stop_server(server2)
    finally:
        if not task.done():
            await wd.stop_client(client, task)

    report = pc.run_case(case, 53, "wifi", [
        pc.snap(0, sink.frames[0]),
        pc.snap(5000, sink.frames[1]),
        pc.probe(6000),
    ], binary=harness_bin)

    assert pc.applied_pairs(report) == [("run-1", 1), ("run-1", 2)], "两份全量都应被应用"
    assert report["rounds"][-1]["view"]["activity"] == "after-drop-latest", \
        "终点必须收敛到重连后的最新快照"
    assert report["ui_no_regress"], report["no_regress_notes"]
    # Python 侧连接可观测：connected → disconnected → connected
    states = links.states()
    assert states.count("connected") >= 2 and "disconnected" in states, states
    first_dc = states.index("disconnected")
    assert "connected" in states[first_dc:], "断连后必须观测到恢复"
    pc.dump_json(os.path.join(d, "wss_trace.json"), {
        "link_states": states,
        "retry_delays_s": client.stats.retry_delays_s,
        "close_codes": client.stats.close_codes,
        "server2_port": port,
    })
    print("[T2] PASS：WSS 断连退避重连，取得最新全量，链路状态可观测")


def test_t2_wss_disconnect_backoff_latest_full(harness_bin: str) -> None:
    asyncio.run(_t2_amain(harness_bin))
