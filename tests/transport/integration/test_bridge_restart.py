"""P3.5 故障矩阵·Bridge 重启（R1–R2）：新 epoch 全量替换 / epoch+seq 语义。

验收锚点（DEVELOPMENT_PLAN P3.5 行 + INTERFACES §3 bridge_epoch/seq 行 + §6）：
  - Bridge 重启生成新 bridge_epoch → 全量替换语义：旧线程状态被新快照整体
    覆盖，设备端无混合；
  - 新 epoch 的低 seq 快照必须接受（epoch 重置 seq 上下文）；
  - 同 epoch 回退 seq 拒绝（IGNORED_STALE_SEQ）；
  - WSS 同端口重启（Bridge 进程重启拓扑）+ 设备退避重连（§6）。
"""

from __future__ import annotations

import asyncio
import os

from bridge.transports.wss import LinkState

import p35_common as pc
import wss_driver as wd


# ---------------------------------------------------------------------------
# R1：Bridge 重启 → 新 epoch 全量替换（旧线程被整体覆盖，无混合）
# ---------------------------------------------------------------------------


async def _r1_amain(harness_bin: str) -> None:
    case = "R1_wss_bridge_restart_new_epoch_full_replacement"
    d = pc.case_dir(case)
    old = pc.make_state(epoch="bridge-run-1", seq=7, thread_id="thread-old",
                        project="proj-old", activity="old-thread-activity",
                        state="working")
    new = pc.make_state(epoch="bridge-run-2", seq=3, thread_id="thread-new",
                        project="proj-new", activity="new-thread-activity",
                        state="needs_you")
    holder = {"snap": old}

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
        assert sink.frames[0] == old

        # Bridge 重启：进程停止（close 1000）→ 同端口以新 epoch 快照恢复
        holder["snap"] = new
        await server1.stop()
        await wd.wait_link(client, LinkState.DISCONNECTED)
        server2 = await wd.start_server(provider=provider, port=port)
        await wd.wait_connected(client, timeout=10.0)
        await sink.wait_count(2)
        assert sink.frames[1] == new, "重启后首帧必须是新 epoch 全量"
        await wd.stop_client(client, task)
        await wd.stop_server(server2)
    finally:
        if not task.done():
            await wd.stop_client(client, task)

    report = pc.run_case(case, 53, "wifi", [
        pc.snap(0, old),
        pc.snap(5000, new),   # 新 epoch、更低 seq → 必须接受并整体替换
        pc.probe(6000),
    ], binary=harness_bin)

    rounds = report["rounds"]
    assert rounds[1]["apply"] == "applied", "新 epoch 的低 seq 快照必须接受（epoch 重置 seq）"
    applied = report["applied"]
    assert [(a["epoch"], a["seq"]) for a in applied] == \
        [("bridge-run-1", 7), ("bridge-run-2", 3)], "epoch 切换必须重置 seq 上下文"
    assert applied[0]["threads"] == ["thread-old"]
    assert applied[1]["threads"] == ["thread-new"], "新快照必须整体替换线程集合"
    assert set(applied[0]["threads"]) & set(applied[1]["threads"]) == set(), \
        "设备端不得出现新旧线程混合"
    view = rounds[-1]["view"]
    assert view["project"] == "proj-new" and view["activity"] == "new-thread-activity", \
        "UI 必须只显示新 epoch 内容"
    assert view["status"] == "NEEDS YOU", "新快照的业务状态必须生效"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)  # 虚拟 5s 间隙无新鲜度告警

    states = links.states()
    assert states.count("connected") >= 2 and "disconnected" in states, states
    pc.dump_json(os.path.join(d, "wss_trace.json"), {
        "link_states": states,
        "retry_delays_s": client.stats.retry_delays_s,
        "restart_port": port,
    })
    print("[R1] PASS：Bridge 重启新 epoch 全量替换，线程集合整体覆盖无混合")


# ---------------------------------------------------------------------------
# R2：同 epoch 回退 seq 拒绝 + 新 epoch 低 seq 接受（单会话注入）
# ---------------------------------------------------------------------------


async def _r2_amain(harness_bin: str) -> None:
    case = "R2_epoch_seq_semantics_regression_and_reset"
    d = pc.case_dir(case)
    s5 = pc.make_state(epoch="run-1", seq=5, activity="five")
    s3 = pc.make_state(epoch="run-1", seq=3, activity="three-regressed")
    s_new = pc.make_state(epoch="run-2", seq=1, activity="epoch-two")

    sink, links = wd.FrameSink(), wd.LinkLog()
    server = await wd.start_server()
    client = wd.make_client(wd.ws_url(server), sink=sink, links=links)
    task = asyncio.create_task(client.run())
    try:
        await wd.wait_connected(client)
        for s in (s5, s3, s_new):
            await wd.send_snapshot(server, s)
        await sink.wait_count(3)
    finally:
        await wd.stop_client(client, task)
        await wd.stop_server(server)

    report = pc.run_case(case, 53, "wifi", [
        pc.snap(0, s5),
        pc.snap(1000, s3),    # 同 epoch 回退 → 拒绝
        pc.snap(2000, s_new),  # 新 epoch 低 seq → 接受
        pc.probe(3000),
    ], binary=harness_bin)

    deliveries = [r["apply"] for r in report["rounds"]]
    assert deliveries == ["applied", "ignored_stale_seq", "applied", "none"], deliveries
    assert pc.applied_pairs(report) == [("run-1", 5), ("run-2", 1)]
    assert report["rounds"][-1]["view"]["activity"] == "epoch-two"
    assert report["ui_no_regress"], report["no_regress_notes"]
    pc.assert_link_trace(report, [(0, "connected")], case)
    pc.dump_json(os.path.join(d, "wss_trace.json"),
                 {"messages_received": client.stats.messages_received,
                  "link_states": links.states()})
    print("[R2] PASS：同 epoch 回退 seq 拒绝；新 epoch seq=1 接受（重置）")


def test_r1_wss_bridge_restart_new_epoch(harness_bin: str) -> None:
    asyncio.run(_r1_amain(harness_bin))


def test_r2_epoch_seq_semantics(harness_bin: str) -> None:
    asyncio.run(_r2_amain(harness_bin))
